from __future__ import annotations

import os

import pytest

from quarksync.backend.webdav import OpenListWebDAVBackend


@pytest.mark.large_file
@pytest.mark.skipif(
    os.getenv("QUARKSYNC_RUN_LARGE_TESTS") != "1",
    reason="set QUARKSYNC_RUN_LARGE_TESTS=1 for the 1 GiB streaming test",
)
async def test_one_gib_upload_iterator_has_constant_memory_chunks(tmp_path):
    source = tmp_path / "one-gib.bin"
    with source.open("wb") as stream:
        stream.seek(1024**3 - 1)
        stream.write(b"\0")
    backend = OpenListWebDAVBackend(
        "http://127.0.0.1/dav/", "unused", "unused", chunk_size=1024 * 1024
    )
    total = 0
    largest = 0
    try:
        async for chunk in backend._file_chunks(source):
            total += len(chunk)
            largest = max(largest, len(chunk))
    finally:
        await backend.close()
    assert total == 1024**3
    assert largest == 1024 * 1024

