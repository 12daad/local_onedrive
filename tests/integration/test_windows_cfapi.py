from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarksync.ipc.client import request
from quarksync.ipc.protocol import PROVIDER_PIPE

pytestmark = [
    pytest.mark.windows_cfapi,
    pytest.mark.skipif(
        os.getenv("QUARKSYNC_RUN_CFAPI_TESTS") != "1",
        reason="requires a registered disposable CFAPI test root and running provider",
    ),
]


async def test_provider_is_connected_and_hydrates_configured_probe():
    status = await request(PROVIDER_PIPE, "STATUS")
    assert status["connected"] is True
    probe = os.getenv("QUARKSYNC_CFAPI_PROBE", "test/hello.txt")
    path = Path(str(status["root"])) / Path(probe)
    content = await __import__("asyncio").to_thread(path.read_bytes)
    assert content

