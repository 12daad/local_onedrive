from __future__ import annotations

import base64
import hashlib
import html
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import pytest


@dataclass
class VirtualFile:
    size: int
    pattern: bytes = b"0123456789abcdef"
    _etag: str | None = None

    def chunks(self, start: int = 0, length: int | None = None, chunk_size: int = 1024 * 1024):
        remaining = self.size - start if length is None else min(length, self.size - start)
        offset = start
        while remaining > 0:
            take = min(chunk_size, remaining)
            phase = offset % len(self.pattern)
            repeated = self.pattern * ((phase + take + len(self.pattern) - 1) // len(self.pattern))
            yield repeated[phase : phase + take]
            offset += take
            remaining -= take

    @property
    def etag(self) -> str:
        if self._etag is None:
            digest = hashlib.sha256()
            for chunk in self.chunks():
                digest.update(chunk)
            self._etag = '"' + digest.hexdigest() + '"'
        return self._etag


@dataclass
class WebDAVState:
    files: dict[str, bytes | VirtualFile] = field(default_factory=dict)
    directories: set[str] = field(default_factory=lambda: {"", "quark"})
    username: str = "syncuser"
    password: str = "secret"  # noqa: S105 - fake fixture credential
    requests: list[tuple[str, str]] = field(default_factory=list)
    delay_seconds: float = 0
    move_status: int | None = None
    hide_after_put: int = 0
    hidden_propfinds: dict[str, int] = field(default_factory=dict)

    def normalize(self, raw: str) -> str:
        path = unquote(urlsplit(raw).path)
        if not path.startswith("/dav"):
            raise ValueError(path)
        return path[4:].strip("/")

    def etag(self, path: str) -> str:
        value = self.files[path]
        if isinstance(value, VirtualFile):
            return value.etag
        return '"' + hashlib.sha256(value).hexdigest() + '"'

    def size(self, path: str) -> int:
        value = self.files[path]
        return value.size if isinstance(value, VirtualFile) else len(value)


class MockWebDAVHandler(BaseHTTPRequestHandler):
    server: MockWebDAVHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:
        pass

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(
            f"{self.server.state.username}:{self.server.state.password}".encode()
        ).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _start(self) -> str | None:
        if not self._authorized():
            return None
        path = self.server.state.normalize(self.path)
        self.server.state.requests.append((self.command, path))
        if self.server.state.delay_seconds:
            time.sleep(self.server.state.delay_seconds)
        return path

    def _empty(self, status: int, **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        if self._start() is None:
            return
        self._empty(
            200,
            Allow="OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE",
            DAV="1, 2",
            Accept_Ranges="bytes",
        )

    def _entry_xml(self, path: str, is_dir: bool) -> str:
        display = html.escape(PurePosixPath(path).name if path else "dav")
        href = "/dav/" + "/".join(quote(part, safe="") for part in path.split("/"))
        if is_dir:
            href = href.rstrip("/") + "/"
            resource = "<d:collection/>"
            size = ""
            etag = ""
        else:
            resource = ""
            size = f"<d:getcontentlength>{self.server.state.size(path)}</d:getcontentlength>"
            etag = f"<d:getetag>{self.server.state.etag(path)}</d:getetag>"
        return (
            "<d:response>"
            f"<d:href>{href}</d:href>"
            "<d:propstat><d:prop>"
            f"<d:displayname>{display}</d:displayname>{size}{etag}"
            "<d:getlastmodified>Sun, 27 Sep 2026 05:00:00 GMT</d:getlastmodified>"
            f"<d:resourcetype>{resource}</d:resourcetype>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    def do_PROPFIND(self) -> None:
        path = self._start()
        if path is None:
            return
        if self.server.state.hidden_propfinds.get(path, 0) > 0:
            self.server.state.hidden_propfinds[path] -= 1
            self._empty(404)
            return
        if path not in self.server.state.directories and path not in self.server.state.files:
            self._empty(404)
            return
        entries = [(path, path in self.server.state.directories)]
        if self.headers.get("Depth") == "1" and path in self.server.state.directories:
            for directory in sorted(self.server.state.directories):
                parent = str(PurePosixPath(directory).parent)
                parent = "" if parent == "." else parent
                if directory and parent == path:
                    entries.append((directory, True))
            for file_path in sorted(self.server.state.files):
                parent = str(PurePosixPath(file_path).parent)
                parent = "" if parent == "." else parent
                if parent == path:
                    entries.append((file_path, False))
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:">'
            + "".join(self._entry_xml(item, is_dir) for item, is_dir in entries)
            + "</d:multistatus>"
        ).encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        path = self._start()
        if path is None:
            return
        if path not in self.server.state.files:
            self._empty(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(self.server.state.size(path)))
        self.send_header("ETag", self.server.state.etag(path))
        self.end_headers()

    def do_GET(self) -> None:
        path = self._start()
        if path is None:
            return
        if path not in self.server.state.files:
            self._empty(404)
            return
        value = self.server.state.files[path]
        if_match = self.headers.get("If-Match")
        if if_match and if_match != self.server.state.etag(path):
            self._empty(412)
            return
        status = 200
        content_range = None
        start = 0
        length = self.server.state.size(path)
        if range_header := self.headers.get("Range"):
            start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
            start, end = int(start_text), int(end_text)
            total = self.server.state.size(path)
            length = min(end - start + 1, total - start)
            status = 206
            content_range = f"bytes {start}-{start + length - 1}/{total}"
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", self.server.state.etag(path))
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        if isinstance(value, VirtualFile):
            for chunk in value.chunks(start, length):
                self.wfile.write(chunk)
        else:
            self.wfile.write(value[start : start + length])

    def do_PUT(self) -> None:
        path = self._start()
        if path is None:
            return
        current = self.headers.get("If-Match")
        if current and (
            path not in self.server.state.files
            or current != self.server.state.etag(path)
        ):
            self._empty(412)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.server.state.files[path] = self.rfile.read(length)
        if self.server.state.hide_after_put:
            self.server.state.hidden_propfinds[path] = self.server.state.hide_after_put
        parent = str(PurePosixPath(path).parent)
        self.server.state.directories.add("" if parent == "." else parent)
        self._empty(201)

    def do_MKCOL(self) -> None:
        path = self._start()
        if path is None:
            return
        if path in self.server.state.directories:
            self._empty(405)
            return
        self.server.state.directories.add(path)
        self._empty(201)

    def do_DELETE(self) -> None:
        path = self._start()
        if path is None:
            return
        existed = self.server.state.files.pop(path, None) is not None
        if path in self.server.state.directories:
            existed = True
            prefix = path + "/"
            self.server.state.directories = {
                item
                for item in self.server.state.directories
                if item != path and not item.startswith(prefix)
            }
            self.server.state.files = {
                item: value
                for item, value in self.server.state.files.items()
                if not item.startswith(prefix)
            }
        self._empty(204 if existed else 404)

    def do_MOVE(self) -> None:
        source = self._start()
        if source is None:
            return
        if self.server.state.move_status is not None:
            self._empty(self.server.state.move_status)
            return
        destination = self.server.state.normalize(self.headers["Destination"])
        if source in self.server.state.directories:
            if (
                destination in self.server.state.directories
                and self.headers.get("Overwrite") != "T"
            ):
                self._empty(412)
                return
            directory_moves = {
                path: destination + path[len(source) :]
                for path in self.server.state.directories
                if path == source or path.startswith(source + "/")
            }
            file_moves = {
                path: destination + path[len(source) :]
                for path in self.server.state.files
                if path.startswith(source + "/")
            }
            for old, new in directory_moves.items():
                self.server.state.directories.discard(old)
                self.server.state.directories.add(new)
            for old, new in file_moves.items():
                self.server.state.files[new] = self.server.state.files.pop(old)
            self._empty(201)
            return
        if source not in self.server.state.files:
            self._empty(404)
            return
        if destination in self.server.state.files and self.headers.get("Overwrite") != "T":
            self._empty(412)
            return
        self.server.state.files[destination] = self.server.state.files.pop(source)
        self._empty(201)


class MockWebDAVHTTPServer(ThreadingHTTPServer):
    state: WebDAVState
    daemon_threads = True
    block_on_close = False


@pytest.fixture
def mock_webdav_server():
    state = WebDAVState()
    server = MockWebDAVHTTPServer(("127.0.0.1", 0), MockWebDAVHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/dav/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
