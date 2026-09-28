from __future__ import annotations

import os

import pytest

from quarksync.cache.manager import CacheManager, CachePressureError
from quarksync.db.database import Database


def manager(tmp_path, *, max_bytes=100):
    database = Database(tmp_path / "state.db")
    database.migrate()
    return CacheManager(
        tmp_path / "cache", database, max_bytes=max_bytes, min_free_bytes=0
    ), database


def test_protected_dirty_data_is_never_evicted(tmp_path):
    cache, _ = manager(tmp_path, max_bytes=1)
    source = tmp_path / "dirty.bin"
    source.write_bytes(os.urandom(1024))
    cache_key, staged = cache.stage_upload(source, "dirty.bin")
    with pytest.raises(CachePressureError):
        cache.evict()
    assert staged.exists()
    with pytest.raises(PermissionError):
        cache.remove(cache_key)


def test_unprotected_lru_data_can_be_evicted(tmp_path):
    cache, _ = manager(tmp_path, max_bytes=1)
    cache_key, path = cache.allocate("remote.bin", "hydration", protected=False)
    path.write_bytes(b"1234")
    cache.record_size(cache_key)
    assert cache.evict() == 1
    assert not path.exists()


def test_restart_recovery_only_removes_hydration_cache(tmp_path):
    cache, database = manager(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"dirty")
    upload_key, upload = cache.stage_upload(source, "dirty.bin")
    hydration_key, hydration = cache.allocate("remote.bin", "hydration", protected=True)
    hydration.write_bytes(b"partial")
    cache.record_size(hydration_key)

    assert cache.recover_transient_hydrations() == 1
    assert not hydration.exists()
    assert upload.exists()
    with database.connect() as connection:
        assert connection.execute(
            "SELECT protected FROM cache_entries WHERE cache_key=?", (upload_key,)
        ).fetchone()[0] == 1
