from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from quarksync.backend.webdav import OpenListWebDAVBackend
from quarksync.cache.manager import CacheManager
from quarksync.db.database import Database
from quarksync.platform.adapter import Placeholder
from quarksync.sync.engine import SyncEngine
from quarksync.sync.scanner_local import LocalSafetyScanner


@dataclass
class FakePlatform:
    placeholders: list[Placeholder] = field(default_factory=list)
    states: dict[str, bool] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    async def create_placeholders(self, entries):
        self.placeholders.extend(entries)

    async def update_placeholder(self, entry):
        self.placeholders.append(entry)

    async def remove_placeholder(self, relative_path):
        self.removed.append(relative_path)

    async def set_in_sync(self, relative_path, in_sync):
        self.states[relative_path] = in_sync


async def make_engine(tmp_path, url):
    database = Database(tmp_path / "state.db")
    backend = OpenListWebDAVBackend(url, "syncuser", "secret", remote_root="quark")
    cache = CacheManager(
        tmp_path / "cache",
        database,
        max_bytes=1024 * 1024 * 100,
        min_free_bytes=0,
    )
    platform = FakePlatform()
    engine = SyncEngine(
        database=database,
        backend=backend,
        cache=cache,
        sync_root=tmp_path / "root",
        device_name="TEST-PC",
        writeback_delay_seconds=0,
        delete_grace_seconds=0,
        max_backoff_seconds=30,
        platform=platform,
    )
    engine.sync_root.mkdir()
    await engine.initialize()
    return engine, backend, database, platform


async def test_new_local_file_is_staged_uploaded_and_marked_in_sync(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        local = engine.sync_root / "new.txt"
        local.write_text("local-first", encoding="utf-8")
        await engine.local_modified("new.txt")
        assert platform.states["new.txt"] is False
        assert await engine.process_one()
        assert state.files["quark/new.txt"] == b"local-first"
        assert platform.states["new.txt"] is True
        with database.connect() as connection:
            row = connection.execute(
                "SELECT sync_state, dirty FROM files WHERE path_key='new.txt'"
            ).fetchone()
            protected = connection.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE protected=1"
            ).fetchone()[0]
        assert tuple(row) == ("SYNCED", 0)
        assert protected == 0
    finally:
        await backend.close()


async def test_local_directory_create_rename_delete_is_durable(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        original = engine.sync_root / "资料"
        original.mkdir()
        await engine.local_created("资料", is_directory=True)
        assert engine.operations.pending()[0].type.value == "MKDIR"
        assert await engine.process_one()
        assert "quark/资料" in state.directories
        assert platform.states["资料"] is True

        renamed = engine.sync_root / "归档"
        original.rename(renamed)
        await engine.local_renamed("资料", "归档")
        assert platform.states["归档"] is False
        assert await engine.process_one()
        assert "quark/资料" not in state.directories
        assert "quark/归档" in state.directories
        assert platform.states["归档"] is True

        renamed.rmdir()
        await engine.local_deleted("归档")
        assert await engine.process_one()
        assert "quark/归档" not in state.directories
        with database.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM files WHERE path_key=?", ("归档".casefold(),)
            ).fetchone()[0] == 0
    finally:
        await backend.close()


async def test_new_local_file_renamed_before_first_upload_skips_remote_move(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    engine, backend, _, _ = await make_engine(tmp_path, url)
    try:
        source = engine.sync_root / "draft.txt"
        destination = engine.sync_root / "final.txt"
        source.write_bytes(b"local-only")
        upload_id = await engine.local_modified("draft.txt")
        source.rename(destination)

        assert await engine.local_renamed("draft.txt", "final.txt") == upload_id
        pending = engine.operations.pending()
        assert [(item.type.value, item.path) for item in pending] == [
            ("UPLOAD", "final.txt")
        ]
        assert await engine.process_one()
        assert "quark/draft.txt" not in state.files
        assert state.files["quark/final.txt"] == b"local-only"
    finally:
        await backend.close()


async def test_remote_directory_rename_rewrites_journal_subtree(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.directories.add("quark/source")
    state.files["quark/source/child.txt"] = b"base"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        source = engine.sync_root / "source"
        source.mkdir()
        (source / "child.txt").write_bytes(b"base")
        destination = engine.sync_root / "destination"
        source.rename(destination)

        await engine.local_renamed("source", "destination")
        with database.connect() as connection:
            paths = [
                row[0]
                for row in connection.execute(
                    "SELECT relative_path FROM files ORDER BY relative_path"
                )
            ]
        assert paths == ["destination", "destination/child.txt"]
        assert await engine.process_one()
        assert "quark/source" not in state.directories
        assert "quark/destination" in state.directories
        assert state.files["quark/destination/child.txt"] == b"base"

        child = destination / "child.txt"
        child.write_bytes(b"edited-after-rename")
        await engine.local_modified("destination/child.txt")
        assert await engine.process_one()
        assert state.files["quark/destination/child.txt"] == b"edited-after-rename"
    finally:
        await backend.close()


async def test_local_directory_delete_uses_one_grace_tombstone_for_subtree(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.directories.add("quark/tree")
    state.files["quark/tree/child.txt"] = b"base"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        operation_id = await engine.local_deleted("tree")
        assert await engine.local_deleted("tree/child.txt") == operation_id
        pending = engine.operations.pending()
        assert [(item.type.value, item.path) for item in pending] == [
            ("DELETE", "tree")
        ]
        with database.connect() as connection:
            states = {
                row[0]
                for row in connection.execute(
                    "SELECT sync_state FROM files WHERE path_key LIKE 'tree%'"
                )
            }
        assert states == {"DELETE_PENDING"}

        assert await engine.process_one()
        assert "quark/tree" not in state.directories
        assert "quark/tree/child.txt" not in state.files
        with database.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM files WHERE path_key LIKE 'tree%'"
            ).fetchone()[0] == 0
    finally:
        await backend.close()


async def test_remote_appearance_causes_keep_both_conflict(tmp_path, mock_webdav_server):
    state, url = mock_webdav_server
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        local = engine.sync_root / "report.docx"
        local.write_bytes(b"local")
        await engine.local_modified("report.docx")
        state.files["quark/report.docx"] = b"remote"
        await engine.process_one()
        conflict_files = list(engine.sync_root.glob("report (conflict TEST-PC *).docx"))
        assert len(conflict_files) == 1
        assert conflict_files[0].read_bytes() == b"local"
        assert state.files["quark/report.docx"] == b"remote"
        with database.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status='OPEN'"
            ).fetchone()[0] == 1
    finally:
        await backend.close()


async def test_unexpected_offline_error_keeps_durable_upload_pending(
    tmp_path, mock_webdav_server, monkeypatch
):
    _, url = mock_webdav_server
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        local = engine.sync_root / "offline.txt"
        local.write_bytes(b"safe local data")
        await engine.local_modified("offline.txt")

        async def offline(_path):
            raise RuntimeError("network is offline")

        monkeypatch.setattr(backend, "stat", offline)
        assert await engine.process_one()
        pending = engine.operations.pending()
        assert len(pending) == 1
        assert pending[0].path == "offline.txt"
        assert pending[0].retry_count == 1
        with database.connect() as connection:
            protected = connection.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE protected=1"
            ).fetchone()[0]
        assert protected == 1
    finally:
        await backend.close()


async def test_remote_update_advances_base_etag_after_placeholder_update(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/remote.txt"] = b"version-one"
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        state.files["quark/remote.txt"] = b"version-two"
        changed, deleted = await engine.poll_remote_directory("")
        assert (changed, deleted) == (1, 0)
        assert await engine.process_one()
        new_etag = state.etag("quark/remote.txt")
        with database.connect() as connection:
            row = connection.execute(
                "SELECT base_etag, sync_state FROM files WHERE path_key='remote.txt'"
            ).fetchone()
        assert tuple(row) == (new_etag, "REMOTE_ONLY")
        assert platform.placeholders[-1].etag == new_etag
    finally:
        await backend.close()


async def test_restart_enumeration_queues_remote_change_before_snapshot_is_lost(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/offline-change.txt"] = b"before"
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        state.files["quark/offline-change.txt"] = b"after-restart"

        await engine.populate_namespace()

        pending = engine.operations.pending()
        assert len(pending) == 1
        assert pending[0].type.value == "DOWNLOAD"
        assert await engine.process_one()
        with database.connect() as connection:
            row = connection.execute(
                "SELECT base_etag, sync_state FROM files "
                "WHERE path_key='offline-change.txt'"
            ).fetchone()
        assert tuple(row) == (state.etag("quark/offline-change.txt"), "REMOTE_ONLY")
        assert platform.placeholders[-1].etag == state.etag(
            "quark/offline-change.txt"
        )
    finally:
        await backend.close()


async def test_remote_directory_delete_is_queued_deepest_first(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.directories.add("quark/folder")
    state.files["quark/folder/child.txt"] = b"child"
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        state.files.pop("quark/folder/child.txt")
        state.directories.remove("quark/folder")
        changed, deleted = await engine.poll_remote_directory("")
        assert (changed, deleted) == (0, 2)
        pending = engine.operations.pending()
        assert [operation.path for operation in pending] == [
            "folder/child.txt",
            "folder",
        ]
        assert all(operation.type.value == "REMOTE_DELETE" for operation in pending)
        while await engine.process_one():
            pass
        assert platform.removed == ["folder/child.txt", "folder"]
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    finally:
        await backend.close()


async def test_local_child_change_cancels_ancestor_remote_delete(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.directories.add("quark/folder")
    state.files["quark/folder/child.txt"] = b"base"
    engine, backend, database, platform = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        folder = engine.sync_root / "folder"
        folder.mkdir()
        child = folder / "child.txt"
        child.write_bytes(b"local-change")
        state.files.pop("quark/folder/child.txt")
        state.directories.remove("quark/folder")
        await engine.poll_remote_directory("")

        await engine.local_modified("folder/child.txt")

        pending = engine.operations.pending()
        assert [(item.type.value, item.path) for item in pending] == [
            ("UPLOAD", "folder/child.txt")
        ]
        with database.connect() as connection:
            cancelled = connection.execute(
                "SELECT COUNT(*) FROM operations "
                "WHERE type='REMOTE_DELETE' AND status='CANCELLED'"
            ).fetchone()[0]
            parent_state = connection.execute(
                "SELECT sync_state FROM files WHERE path_key='folder'"
            ).fetchone()[0]
        assert cancelled == 2
        assert parent_state == "CONFLICT"
        assert platform.removed == []
    finally:
        await backend.close()


async def test_save_during_upload_keeps_newer_staging_protected(
    tmp_path, mock_webdav_server, monkeypatch
):
    state, url = mock_webdav_server
    engine, backend, database, platform = await make_engine(tmp_path, url)
    started = asyncio.Event()
    release = asyncio.Event()
    real_upload = backend.upload

    async def delayed_upload(source: Path, path: str, *, expected_etag=None):
        started.set()
        await release.wait()
        return await real_upload(source, path, expected_etag=expected_etag)

    monkeypatch.setattr(backend, "upload", delayed_upload)
    try:
        local = engine.sync_root / "frequent.txt"
        local.write_bytes(b"version-one")
        await engine.local_modified("frequent.txt")
        first = asyncio.create_task(engine.process_one())
        await started.wait()

        local.write_bytes(b"version-two-is-newer")
        await engine.local_modified("frequent.txt")
        release.set()
        assert await first

        pending = engine.operations.pending()
        assert len(pending) == 1
        staged = Path(str(pending[0].payload["cache_path"]))
        assert await asyncio.to_thread(staged.read_bytes) == b"version-two-is-newer"
        with database.connect() as connection:
            row = connection.execute(
                "SELECT sync_state, dirty FROM files WHERE path_key='frequent.txt'"
            ).fetchone()
            protected = connection.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE protected=1"
            ).fetchone()[0]
        assert tuple(row) == ("UPLOAD_PENDING", 1)
        assert protected == 1
        assert platform.states["frequent.txt"] is False

        assert await engine.process_one()
        assert state.files["quark/frequent.txt"] == b"version-two-is-newer"
        assert platform.states["frequent.txt"] is True
    finally:
        await backend.close()


async def test_debounced_saves_keep_only_latest_protected_staging(
    tmp_path, mock_webdav_server
):
    _, url = mock_webdav_server
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        local = engine.sync_root / "debounced.txt"
        for version in (b"one", b"two", b"three"):
            local.write_bytes(version)
            await engine.local_modified("debounced.txt")
        pending = engine.operations.pending()
        assert len(pending) == 1
        staged = Path(str(pending[0].payload["cache_path"]))
        assert await asyncio.to_thread(staged.read_bytes) == b"three"
        with database.connect() as connection:
            entries = connection.execute(
                "SELECT COUNT(*), SUM(protected) FROM cache_entries WHERE purpose='uploads'"
            ).fetchone()
        assert tuple(entries) == (1, 1)
    finally:
        await backend.close()


async def test_file_changed_during_staging_is_retried_before_queueing(
    tmp_path, mock_webdav_server, monkeypatch
):
    _, url = mock_webdav_server
    engine, backend, database, _ = await make_engine(tmp_path, url)
    real_stage = engine.cache.stage_upload
    changed_once = False
    try:
        local = engine.sync_root / "active.txt"
        local.write_bytes(b"first")

        def stage_then_change(source: Path, relative_path: str):
            nonlocal changed_once
            result = real_stage(source, relative_path)
            if not changed_once:
                changed_once = True
                source.write_bytes(b"second-version")
            return result

        monkeypatch.setattr(engine.cache, "stage_upload", stage_then_change)
        await engine.local_modified("active.txt")

        pending = engine.operations.pending()
        staged = Path(str(pending[0].payload["cache_path"]))
        assert await asyncio.to_thread(staged.read_bytes) == b"second-version"
        with database.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE purpose='uploads'"
            ).fetchone()[0] == 1
    finally:
        await backend.close()


async def test_restoring_file_during_delete_grace_cancels_remote_delete(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/restored.txt"] = b"remote-base"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        local = engine.sync_root / "restored.txt"
        local.write_bytes(b"remote-base")
        local.unlink()
        await engine.local_deleted("restored.txt")

        local.write_bytes(b"restored-local")
        await engine.local_modified("restored.txt")
        with database.connect() as connection:
            delete_status = connection.execute(
                "SELECT status FROM operations WHERE type='DELETE' AND path='restored.txt'"
            ).fetchone()[0]
        assert delete_status == "CANCELLED"
        assert await engine.process_one()
        assert state.files["quark/restored.txt"] == b"restored-local"
    finally:
        await backend.close()


async def test_safety_scanner_recovers_missed_local_delete(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/missed.txt"] = b"remote"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        with database.transaction() as connection:
            connection.execute(
                "UPDATE files SET hydration_state='HYDRATED' WHERE path_key='missed.txt'"
            )
        scanner = LocalSafetyScanner(engine.sync_root, database, engine)
        assert await scanner.scan() == 1
        pending = engine.operations.pending()
        assert len(pending) == 1
        assert pending[0].type.value == "DELETE"
        assert pending[0].path == "missed.txt"
    finally:
        await backend.close()


async def test_remote_rename_destination_race_is_a_visible_conflict(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/a.txt"] = b"base"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        source = engine.sync_root / "a.txt"
        destination = engine.sync_root / "b.txt"
        source.write_bytes(b"base")
        source.rename(destination)
        await engine.local_renamed("a.txt", "b.txt")
        state.files["quark/b.txt"] = b"other-device"

        assert await engine.process_one()
        with database.connect() as connection:
            file_row = connection.execute(
                "SELECT sync_state FROM files WHERE path_key='b.txt'"
            ).fetchone()
            conflicts = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE path='b.txt' AND status='OPEN'"
            ).fetchone()[0]
            operation = connection.execute(
                "SELECT status FROM operations WHERE type='MOVE'"
            ).fetchone()
        assert file_row["sync_state"] == "CONFLICT"
        assert conflicts == 1
        assert operation["status"] == "FAILED"
        assert state.files["quark/a.txt"] == b"base"
        assert state.files["quark/b.txt"] == b"other-device"
    finally:
        await backend.close()


async def test_remote_change_during_delete_grace_is_a_visible_conflict(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/delete.txt"] = b"base"
    engine, backend, database, _ = await make_engine(tmp_path, url)
    try:
        await engine.populate_namespace()
        await engine.local_deleted("delete.txt")
        state.files["quark/delete.txt"] = b"other-device"

        assert await engine.process_one()
        with database.connect() as connection:
            file_row = connection.execute(
                "SELECT sync_state FROM files WHERE path_key='delete.txt'"
            ).fetchone()
            conflicts = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE path='delete.txt' AND status='OPEN'"
            ).fetchone()[0]
        assert file_row["sync_state"] == "CONFLICT"
        assert conflicts == 1
        assert state.files["quark/delete.txt"] == b"other-device"
    finally:
        await backend.close()
