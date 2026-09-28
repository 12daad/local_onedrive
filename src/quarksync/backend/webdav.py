from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

from quarksync.backend.base import BackendCapabilities, DownloadProgress, RemoteEntry

DAV = "{DAV:}"
logger = logging.getLogger(__name__)
PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:displayname/><d:getcontentlength/><d:getlastmodified/><d:getetag/><d:resourcetype/>
</d:prop></d:propfind>"""


def _first_prop_text(properties: list[Element], name: str) -> str | None:
    for prop in properties:
        value = prop.findtext(f"{DAV}{name}")
        if value is not None:
            return value
    return None


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RemoteNotFoundError(BackendError):
    pass


class RemoteChangedError(BackendError):
    pass


def normalize_remote_path(value: str) -> str:
    value = value.replace("\\", "/").strip("/")
    parts = PurePosixPath(value).parts if value else ()
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Remote paths may not contain dot segments")
    return "/".join(parts)


def path_key(value: str) -> str:
    return normalize_remote_path(value).casefold()


class OpenListWebDAVBackend:
    """Streaming WebDAV adapter; all remote HTTP is contained in this class."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        remote_root: str = "",
        verify: bool = True,
        timeout: float = 60,
        chunk_size: int = 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.remote_root = normalize_remote_path(remote_root)
        self.chunk_size = chunk_size
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            auth=(username, password),
            verify=verify,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15)),
            follow_redirects=True,
            headers={"User-Agent": "QuarkSync/0.1"},
        )
        self.capabilities = BackendCapabilities()

    def _remote_path(self, path: str) -> str:
        normalized = normalize_remote_path(path)
        return "/".join(part for part in (self.remote_root, normalized) if part)

    def _url(self, path: str, *, directory: bool = False) -> str:
        remote_path = self._remote_path(path)
        encoded = "/".join(quote(segment, safe="") for segment in remote_path.split("/") if segment)
        url = urljoin(self.base_url, encoded)
        if directory and not url.endswith("/"):
            url += "/"
        return url

    def _path_from_href(self, href: str) -> str:
        href_path = unquote(urlsplit(href).path)
        base_path = unquote(urlsplit(self.base_url).path).rstrip("/")
        if href_path.rstrip("/") == base_path:
            relative = ""
        elif href_path.startswith(base_path + "/"):
            relative = href_path[len(base_path) + 1 :].strip("/")
        else:
            raise BackendError("Server returned an href outside the configured WebDAV root")
        if self.remote_root:
            if relative == self.remote_root:
                return ""
            prefix = self.remote_root + "/"
            if not relative.startswith(prefix):
                raise BackendError("Server returned an href outside QUARKSYNC_REMOTE_ROOT")
            relative = relative[len(prefix) :]
        return normalize_remote_path(relative)

    @staticmethod
    def _raise(response: httpx.Response, operation: str) -> None:
        if response.status_code == 404:
            raise RemoteNotFoundError(f"{operation}: remote path not found", status_code=404)
        if response.status_code == 412:
            raise RemoteChangedError(
                f"{operation}: remote precondition failed", status_code=response.status_code
            )
        if response.is_error:
            raise BackendError(
                f"{operation}: WebDAV returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    async def detect_capabilities(self) -> BackendCapabilities:
        response = await self.client.request("OPTIONS", self._url("", directory=True))
        if response.status_code in {405, 501}:
            self.capabilities = BackendCapabilities()
            logger.warning(
                "WebDAV OPTIONS is unavailable; using conservative direct-PUT capabilities"
            )
            return self.capabilities
        self._raise(response, "OPTIONS")
        allow = {item.strip().upper() for item in response.headers.get("Allow", "").split(",")}
        self.capabilities = BackendCapabilities(
            head="HEAD" in allow,
            move="MOVE" in allow,
            # WebDAV MOVE can be atomic on OpenList's side but is not guaranteed by RFC 4918.
            atomic_move=False,
            ranges="bytes" in response.headers.get("Accept-Ranges", "").lower(),
        )
        logger.info(
            "WebDAV capabilities: HEAD=%s MOVE=%s ranges=%s atomic_MOVE=%s",
            self.capabilities.head,
            self.capabilities.move,
            self.capabilities.ranges,
            self.capabilities.atomic_move,
        )
        return self.capabilities

    async def _propfind(
        self, path: str, depth: int, *, directory: bool = False
    ) -> list[RemoteEntry]:
        response = await self.client.request(
            "PROPFIND",
            self._url(path, directory=directory),
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
            content=PROPFIND_BODY,
        )
        self._raise(response, "PROPFIND")
        if response.status_code != 207:
            raise BackendError(
                f"PROPFIND: expected HTTP 207, got {response.status_code}",
                status_code=response.status_code,
            )
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise BackendError("PROPFIND: malformed XML response") from exc
        entries: list[RemoteEntry] = []
        for item in root.findall(f"{DAV}response"):
            href_node = item.find(f"{DAV}href")
            if href_node is None or not href_node.text:
                continue
            successful_props = []
            for propstat in item.findall(f"{DAV}propstat"):
                status = propstat.findtext(f"{DAV}status", "")
                if " 200 " in status:
                    prop = propstat.find(f"{DAV}prop")
                    if prop is not None:
                        successful_props.append(prop)
            if not successful_props:
                continue

            is_dir = any(
                (resource_type := prop.find(f"{DAV}resourcetype")) is not None
                and resource_type.find(f"{DAV}collection") is not None
                for prop in successful_props
            )
            size_text = _first_prop_text(successful_props, "getcontentlength")
            size = None
            with contextlib.suppress(ValueError, TypeError):
                size = int(size_text) if size_text is not None else None
            entries.append(
                RemoteEntry(
                    path=self._path_from_href(href_node.text),
                    href=href_node.text,
                    display_name=_first_prop_text(successful_props, "displayname"),
                    is_dir=is_dir,
                    size=None if is_dir else size,
                    mtime=_first_prop_text(successful_props, "getlastmodified"),
                    etag=_first_prop_text(successful_props, "getetag"),
                )
            )
        return entries

    async def stat(self, path: str) -> RemoteEntry:
        entries = await self._propfind(path, 0)
        if not entries:
            raise RemoteNotFoundError("PROPFIND: remote path not found", status_code=404)
        return entries[0]

    async def _stat_after_write(self, path: str) -> RemoteEntry:
        """Allow eventually-consistent WebDAV drivers to expose a completed PUT."""
        delays = (0.1, 0.25, 0.5, 1.0, 2.0, 2.0, 2.0)
        for delay in delays:
            try:
                return await self.stat(path)
            except RemoteNotFoundError:
                await asyncio.sleep(delay)
        return await self.stat(path)

    async def list_dir(self, path: str) -> list[RemoteEntry]:
        parent_key = path_key(path)
        return [
            entry
            for entry in await self._propfind(path, 1, directory=True)
            if path_key(entry.path) != parent_key
        ]

    async def download(
        self,
        path: str,
        destination: Path,
        *,
        expected_etag: str | None = None,
        progress: DownloadProgress | None = None,
    ) -> RemoteEntry:
        before = await self.stat(path)
        etag = expected_etag or before.etag
        headers = {"If-Match": etag} if etag else {}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
        completed = 0
        total = before.size or 0
        try:
            async with self.client.stream("GET", self._url(path), headers=headers) as response:
                self._raise(response, "GET")
                with temporary.open("wb", buffering=0) as output:
                    async for chunk in response.aiter_bytes(self.chunk_size):
                        await asyncio.to_thread(output.write, chunk)
                        completed += len(chunk)
                        if progress is not None and total > 0:
                            await progress(completed, total)
                    await asyncio.to_thread(os.fsync, output.fileno())
            after = await self.stat(path)
            if etag and after.etag and after.etag != etag:
                raise RemoteChangedError("Remote file changed during download")
            if not etag and (
                before.size is not None
                and after.size is not None
                and before.size != after.size
                or before.mtime is not None
                and after.mtime is not None
                and before.mtime != after.mtime
            ):
                raise RemoteChangedError(
                    "Remote metadata changed during an ETag-less download"
                )
            if before.size is not None and temporary.stat().st_size != before.size:
                raise BackendError("Downloaded size does not match WebDAV metadata")
            os.replace(temporary, destination)
            return after
        finally:
            temporary.unlink(missing_ok=True)

    async def download_range(
        self, path: str, start: int, length: int, destination: Path
    ) -> None:
        if start < 0 or length <= 0:
            raise ValueError("Invalid byte range")
        end = start + length - 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self.client.stream(
            "GET", self._url(path), headers={"Range": f"bytes={start}-{end}"}
        ) as response:
            self._raise(response, "range GET")
            if response.status_code != 206:
                raise BackendError(
                    f"Range GET: expected HTTP 206, got {response.status_code}",
                    status_code=response.status_code,
                )
            with destination.open("wb", buffering=0) as output:
                async for chunk in response.aiter_bytes(self.chunk_size):
                    await asyncio.to_thread(output.write, chunk)

    async def _file_chunks(self, source: Path) -> AsyncIterator[bytes]:
        with source.open("rb", buffering=0) as stream:
            while chunk := await asyncio.to_thread(stream.read, self.chunk_size):
                yield chunk

    async def _put(
        self,
        source: Path,
        path: str,
        *,
        expected_etag: str | None = None,
        create_only: bool = False,
    ) -> None:
        source_stat = await asyncio.to_thread(source.stat)
        headers = {"Content-Length": str(source_stat.st_size)}
        if expected_etag:
            headers["If-Match"] = expected_etag
        elif create_only:
            headers["If-None-Match"] = "*"
        response = await self.client.put(
            self._url(path), headers=headers, content=self._file_chunks(source)
        )
        self._raise(response, "PUT")

    async def upload(
        self,
        source: Path,
        path: str,
        *,
        expected_etag: str | None = None,
    ) -> RemoteEntry:
        if not await asyncio.to_thread(source.is_file):
            raise FileNotFoundError(source)
        if expected_etag:
            current = await self.stat(path)
            if current.etag and current.etag != expected_etag:
                raise RemoteChangedError("Upload refused because the remote ETag changed")
        # WebDAV MOVE has no standard target If-Match precondition. It is safe for
        # a new target with Overwrite:F, but not for replacing a known base version.
        if self.capabilities.move and expected_etag is None:
            name = PurePosixPath(normalize_remote_path(path)).name
            parent = str(PurePosixPath(normalize_remote_path(path)).parent)
            parent = "" if parent == "." else parent
            temporary = "/".join(
                part for part in (parent, f".{name}.quarksync-{uuid.uuid4().hex}.tmp") if part
            )
            await self._put(source, temporary)
            try:
                await self.move(temporary, path, overwrite=False)
            except BackendError as exc:
                with contextlib.suppress(BackendError):
                    await self.delete(temporary)
                self.capabilities = BackendCapabilities(
                    head=self.capabilities.head,
                    move=False,
                    atomic_move=False,
                    ranges=self.capabilities.ranges,
                )
                try:
                    await self.stat(path)
                except RemoteNotFoundError:
                    logger.warning(
                        "WebDAV MOVE failed with HTTP %s; disabling MOVE and using "
                        "a create-only direct PUT",
                        exc.status_code,
                    )
                    await self._put(source, path, create_only=True)
                else:
                    raise RemoteChangedError(
                        "MOVE failed and the destination appeared; refusing to overwrite it"
                    ) from exc
            except BaseException:
                with contextlib.suppress(BackendError):
                    await self.delete(temporary)
                raise
        else:
            await self._put(source, path, expected_etag=expected_etag)
        return await self._stat_after_write(path)

    async def mkdir(self, path: str) -> None:
        response = await self.client.request("MKCOL", self._url(path, directory=True))
        if response.status_code not in {201, 204, 405}:
            self._raise(response, "MKCOL")

    async def delete(self, path: str) -> None:
        response = await self.client.delete(self._url(path))
        if response.status_code != 404:
            self._raise(response, "DELETE")

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> None:
        response = await self.client.request(
            "MOVE",
            self._url(source),
            headers={
                "Destination": self._url(destination),
                "Overwrite": "T" if overwrite else "F",
            },
        )
        self._raise(response, "MOVE")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> OpenListWebDAVBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
