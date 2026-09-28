from __future__ import annotations

from dataclasses import asdict

from quarksync.ipc.client import request
from quarksync.ipc.protocol import PROVIDER_PIPE
from quarksync.platform.adapter import Placeholder


class IpcPlatformAdapter:
    def __init__(self, *, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = timeout_seconds

    async def ping(self) -> None:
        await request(PROVIDER_PIPE, "PING", timeout_seconds=1)

    async def create_placeholders(self, entries: list[Placeholder]) -> None:
        await request(
            PROVIDER_PIPE,
            "CREATE_PLACEHOLDERS",
            {"entries": [asdict(entry) for entry in entries]},
            timeout_seconds=self.timeout_seconds,
        )

    async def update_placeholder(self, entry: Placeholder) -> None:
        await request(
            PROVIDER_PIPE,
            "UPDATE_PLACEHOLDER",
            asdict(entry),
            timeout_seconds=self.timeout_seconds,
        )

    async def remove_placeholder(self, relative_path: str) -> None:
        await request(
            PROVIDER_PIPE,
            "REMOVE_PLACEHOLDER",
            {"path": relative_path},
            timeout_seconds=self.timeout_seconds,
        )

    async def set_in_sync(self, relative_path: str, in_sync: bool) -> None:
        await request(
            PROVIDER_PIPE,
            "SET_IN_SYNC",
            {"path": relative_path, "in_sync": in_sync},
            timeout_seconds=self.timeout_seconds,
        )

    async def report_progress(
        self, transfer_id: str, total: int, completed: int
    ) -> None:
        await request(
            PROVIDER_PIPE,
            "REPORT_PROGRESS",
            {"transfer_id": transfer_id, "total": total, "completed": completed},
            timeout_seconds=min(self.timeout_seconds, 2),
        )
