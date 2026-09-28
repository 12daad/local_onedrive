from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DownloadProgress = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    path: str
    href: str
    display_name: str | None
    is_dir: bool
    size: int | None = None
    mtime: str | None = None
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    head: bool = False
    move: bool = False
    atomic_move: bool = False
    ranges: bool = False


class StorageBackend(Protocol):
    async def detect_capabilities(self) -> BackendCapabilities: ...

    async def stat(self, path: str) -> RemoteEntry: ...

    async def list_dir(self, path: str) -> list[RemoteEntry]: ...

    async def download(
        self,
        path: str,
        destination: Path,
        *,
        expected_etag: str | None = None,
        progress: DownloadProgress | None = None,
    ) -> RemoteEntry: ...

    async def download_range(
        self, path: str, start: int, length: int, destination: Path
    ) -> None: ...

    async def upload(
        self,
        source: Path,
        path: str,
        *,
        expected_etag: str | None = None,
    ) -> RemoteEntry: ...

    async def mkdir(self, path: str) -> None: ...

    async def delete(self, path: str) -> None: ...

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> None: ...

    async def close(self) -> None: ...
