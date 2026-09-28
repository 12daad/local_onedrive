from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Placeholder:
    relative_path: str
    is_directory: bool
    size: int | None
    mtime: str | None
    etag: str | None


class PlatformAdapter(Protocol):
    async def create_placeholders(self, entries: list[Placeholder]) -> None: ...

    async def update_placeholder(self, entry: Placeholder) -> None: ...

    async def remove_placeholder(self, relative_path: str) -> None: ...

    async def set_in_sync(self, relative_path: str, in_sync: bool) -> None: ...

    async def report_progress(
        self, transfer_id: str, total: int, completed: int
    ) -> None: ...
