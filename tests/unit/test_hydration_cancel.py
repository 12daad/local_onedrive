from __future__ import annotations

import asyncio

import pytest

from quarksync.daemon import Daemon
from quarksync.ipc.protocol import Request


class BlockingFetchEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def fetch_file(self, _path: str, _etag: str | None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def test_cancel_fetch_cancels_matching_download_task():
    daemon = object.__new__(Daemon)
    daemon.engine = BlockingFetchEngine()
    daemon.fetch_tasks = {}
    fetch = asyncio.create_task(
        daemon._handle_core(
            Request.create(
                "FETCH_FILE",
                {"path": "large.bin", "etag": '"base"', "transfer_id": "42"},
            )
        )
    )
    await daemon.engine.started.wait()

    result = await daemon._handle_core(
        Request.create("CANCEL_FETCH", {"transfer_id": "42"})
    )

    assert result == {"cancelled": True}
    with pytest.raises(asyncio.CancelledError):
        await fetch
    assert daemon.engine.cancelled.is_set()
    assert daemon.fetch_tasks == {}
