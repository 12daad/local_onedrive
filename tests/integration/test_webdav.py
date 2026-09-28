from __future__ import annotations

import httpx
import pytest

from quarksync.backend.webdav import OpenListWebDAVBackend, RemoteChangedError


@pytest.fixture
async def backend(mock_webdav_server):
    _, url = mock_webdav_server
    value = OpenListWebDAVBackend(url, "syncuser", "secret", remote_root="quark")
    try:
        yield value
    finally:
        await value.close()


async def test_list_stat_and_unicode_encoding(backend, mock_webdav_server):
    state, _ = mock_webdav_server
    state.directories.add("quark/文 档")
    state.files["quark/文 档/a#%&😀.txt"] = "内容".encode()

    entries = await backend.list_dir("文 档")
    assert [entry.path for entry in entries] == ["文 档/a#%&😀.txt"]
    item = await backend.stat("文 档/a#%&😀.txt")
    assert item.size == len("内容".encode())
    assert item.etag


async def test_file_stat_does_not_add_a_directory_slash(backend, mock_webdav_server):
    state, _ = mock_webdav_server
    state.files["quark/schema.json"] = b"{}"

    await backend.stat("schema.json")

    method, path = state.requests[-1]
    assert method == "PROPFIND"
    assert path == "quark/schema.json"


async def test_download_and_range_stream_to_files(backend, mock_webdav_server, tmp_path):
    state, _ = mock_webdav_server
    state.files["quark/large.bin"] = bytes(range(256)) * 8192
    destination = tmp_path / "large.bin"

    metadata = await backend.download("large.bin", destination)
    assert destination.read_bytes() == state.files["quark/large.bin"]
    assert metadata.size == destination.stat().st_size

    ranged = tmp_path / "range.bin"
    await backend.download_range("large.bin", 123, 4096, ranged)
    assert ranged.read_bytes() == state.files["quark/large.bin"][123 : 123 + 4096]


async def test_upload_uses_temporary_put_and_move(backend, mock_webdav_server, tmp_path):
    state, _ = mock_webdav_server
    source = tmp_path / "本地 file.txt"
    source.write_bytes(b"new data")

    capabilities = await backend.detect_capabilities()
    result = await backend.upload(source, "folder/remote.txt")

    assert capabilities.move
    assert state.files["quark/folder/remote.txt"] == b"new data"
    assert result.size == 8
    methods = [method for method, _ in state.requests]
    assert "PUT" in methods and "MOVE" in methods


async def test_upload_falls_back_to_create_only_put_when_move_is_broken(
    backend, mock_webdav_server, tmp_path
):
    state, _ = mock_webdav_server
    state.move_status = 500
    source = tmp_path / "fallback.txt"
    source.write_bytes(b"safe fallback")
    await backend.detect_capabilities()

    result = await backend.upload(source, "fallback.txt")

    assert result.size == len(b"safe fallback")
    assert state.files["quark/fallback.txt"] == b"safe fallback"
    assert backend.capabilities.move is False
    methods = [method for method, _ in state.requests]
    assert methods.count("PUT") == 2
    assert "MOVE" in methods
    assert not any(".quarksync-" in path for path in state.files)


async def test_mkdir_move_delete_and_etag_precondition(backend, mock_webdav_server, tmp_path):
    state, _ = mock_webdav_server
    await backend.mkdir("newdir")
    assert "quark/newdir" in state.directories
    state.files["quark/a.txt"] = b"a"
    await backend.move("a.txt", "b.txt")
    assert state.files["quark/b.txt"] == b"a"

    source = tmp_path / "changed.txt"
    source.write_bytes(b"changed")
    with pytest.raises(RemoteChangedError):
        await backend.upload(source, "b.txt", expected_etag='"wrong"')
    await backend.delete("b.txt")
    assert "quark/b.txt" not in state.files


async def test_existing_upload_uses_atomic_if_match_not_overwriting_move(
    backend, mock_webdav_server, tmp_path
):
    state, _ = mock_webdav_server
    state.files["quark/existing.txt"] = b"base"
    expected_etag = state.etag("quark/existing.txt")
    source = tmp_path / "existing.txt"
    source.write_bytes(b"updated")
    await backend.detect_capabilities()
    state.requests.clear()

    await backend.upload(source, "existing.txt", expected_etag=expected_etag)

    assert state.files["quark/existing.txt"] == b"updated"
    methods = [method for method, _ in state.requests]
    assert "PUT" in methods
    assert "MOVE" not in methods


async def test_existing_upload_retries_stat_until_put_is_visible(
    backend, mock_webdav_server, tmp_path
):
    state, _ = mock_webdav_server
    state.files["quark/eventual.txt"] = b"base"
    expected_etag = state.etag("quark/eventual.txt")
    state.hide_after_put = 2
    source = tmp_path / "eventual.txt"
    source.write_bytes(b"updated")

    result = await backend.upload(source, "eventual.txt", expected_etag=expected_etag)

    assert result.size == len(b"updated")
    assert state.hidden_propfinds["quark/eventual.txt"] == 0


async def test_auth_error_is_reported_without_secret(mock_webdav_server):
    _, url = mock_webdav_server
    backend = OpenListWebDAVBackend(url, "syncuser", "wrong", remote_root="quark")
    try:
        with pytest.raises(Exception) as caught:
            await backend.list_dir("")
        assert "wrong" not in str(caught.value)
    finally:
        await backend.close()


async def test_timeout(mock_webdav_server):
    state, url = mock_webdav_server
    state.delay_seconds = 0.2
    client = httpx.AsyncClient(
        auth=("syncuser", "secret"), timeout=httpx.Timeout(0.05), follow_redirects=True
    )
    backend = OpenListWebDAVBackend(
        url, "syncuser", "secret", remote_root="quark", client=client
    )
    try:
        with pytest.raises(httpx.TimeoutException):
            await backend.list_dir("")
    finally:
        await client.aclose()
