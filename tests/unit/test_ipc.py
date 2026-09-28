from __future__ import annotations

import asyncio
import uuid

from quarksync.ipc.client import request
from quarksync.ipc.protocol import Request
from quarksync.ipc.server import NamedPipeServer


async def test_current_user_named_pipe_round_trip():
    pipe_name = rf"\\.\pipe\QuarkSync.Tests.{uuid.uuid4()}"

    async def handler(message: Request) -> dict[str, object]:
        return {"type": message.type, "value": message.payload["value"]}

    server = NamedPipeServer(pipe_name, handler, asyncio.get_running_loop())
    server.start()
    try:
        result = await request(pipe_name, "PING", {"value": 42}, timeout_seconds=3)
        assert result == {"type": "PING", "value": 42}
    finally:
        server.close()
