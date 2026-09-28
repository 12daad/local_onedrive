from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from quarksync.backend.base import DownloadProgress, RemoteEntry, StorageBackend
from quarksync.backend.webdav import BackendError, RemoteNotFoundError
from quarksync.cache.manager import CacheManager
from quarksync.db.database import Database, utc_now
from quarksync.platform.adapter import Placeholder, PlatformAdapter
from quarksync.sync.conflicts import ConflictManager
from quarksync.sync.operations import (
    Operation,
    OperationQueue,
    OperationSuperseded,
    OperationType,
)
from quarksync.sync.paths import key, local_path, normalize_relative_path
from quarksync.sync.scanner import RemoteScanner
from quarksync.sync.states import SyncState, SyncStateMachine

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        *,
        database: Database,
        backend: StorageBackend,
        cache: CacheManager,
        sync_root: Path,
        device_name: str,
        writeback_delay_seconds: int,
        delete_grace_seconds: int,
        max_backoff_seconds: int,
        platform: PlatformAdapter | None = None,
    ) -> None:
        self.database = database
        self.backend = backend
        self.cache = cache
        self.sync_root = sync_root
        self.platform = platform
        self.states = SyncStateMachine(database)
        self.operations = OperationQueue(
            database, max_backoff_seconds=max_backoff_seconds
        )
        self.conflicts = ConflictManager(database, device_name)
        self.remote_scanner = RemoteScanner(database, backend)
        self.writeback_delay_seconds = writeback_delay_seconds
        self.delete_grace_seconds = delete_grace_seconds

    async def initialize(self) -> None:
        self.database.migrate()
        recovered = self.operations.recover_interrupted()
        if recovered:
            logger.warning("Recovered %d interrupted operations", recovered)
        recovered_hydrations = self.cache.recover_transient_hydrations()
        with self.database.connect() as connection:
            hydrating = [
                str(row[0])
                for row in connection.execute(
                    "SELECT relative_path FROM files WHERE sync_state='HYDRATING'"
                )
            ]
        for relative_path in hydrating:
            self.states.transition(relative_path, SyncState.REMOTE_ONLY, force=True)
        if recovered_hydrations or hydrating:
            logger.warning(
                "Recovered %d transient hydration cache entries and %d file states",
                recovered_hydrations,
                len(hydrating),
            )
        await self.backend.detect_capabilities()

    async def populate_namespace(self) -> int:
        async def create_batch(entries: list[RemoteEntry]) -> None:
            now = utc_now()
            remote_updates: list[RemoteEntry] = []
            remote_conflicts: list[tuple[RemoteEntry, str | None]] = []
            with self.database.transaction() as connection:
                for entry in entries:
                    existing = connection.execute(
                        """
                        SELECT base_etag, remote_size, remote_mtime, dirty
                        FROM files WHERE path_key=?
                        """,
                        (key(entry.path),),
                    ).fetchone()
                    if existing:
                        if entry.etag is not None or existing["base_etag"] is not None:
                            changed = entry.etag != existing["base_etag"]
                        else:
                            changed = (entry.size, entry.mtime) != (
                                existing["remote_size"],
                                existing["remote_mtime"],
                            )
                        if changed and existing["dirty"]:
                            remote_conflicts.append((entry, existing["base_etag"]))
                        elif changed:
                            remote_updates.append(entry)
                    connection.execute(
                        """
                        INSERT INTO files(
                            relative_path, path_key, remote_path, remote_etag,
                            remote_size, remote_mtime, sync_state, hydration_state,
                            base_etag, is_directory, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'REMOTE_ONLY', 'REMOTE_ONLY', ?, ?, ?)
                        ON CONFLICT(path_key) DO UPDATE SET
                            remote_path=excluded.remote_path,
                            remote_etag=excluded.remote_etag,
                            remote_size=excluded.remote_size,
                            remote_mtime=excluded.remote_mtime,
                            is_directory=excluded.is_directory,
                            updated_at=excluded.updated_at
                        """,
                        (
                            entry.path,
                            key(entry.path),
                            entry.path,
                            entry.etag,
                            entry.size,
                            entry.mtime,
                            entry.etag,
                            int(entry.is_dir),
                            now,
                        ),
                    )
            for entry, base_etag in remote_conflicts:
                self._preserve_local_conflict(entry.path, entry.etag, base_etag)
                self.states.transition(entry.path, SyncState.CONFLICT, force=True)
            for entry in remote_updates:
                self.states.transition(entry.path, SyncState.DOWNLOAD_PENDING, force=True)
                self.operations.enqueue(
                    OperationType.DOWNLOAD,
                    entry.path,
                    payload={"etag": entry.etag},
                    debounce=True,
                )
            if self.platform:
                await self.platform.create_placeholders(
                    [self._placeholder(entry) for entry in entries]
                )

        return await self.remote_scanner.eager_enumerate(on_batch=create_batch)

    async def fetch_file(
        self,
        relative_path: str,
        expected_etag: str | None,
        *,
        progress: DownloadProgress | None = None,
    ) -> dict[str, object]:
        relative_path = normalize_relative_path(relative_path)
        metadata = await self.backend.stat(relative_path)
        if metadata.is_dir:
            raise IsADirectoryError(relative_path)
        if expected_etag and metadata.etag and expected_etag != metadata.etag:
            raise BackendError("Placeholder version is stale; refusing hydration")
        self._ensure_file_row(metadata)
        self.states.transition(relative_path, SyncState.HYDRATING, force=True)
        cache_key, destination = self.cache.allocate(
            relative_path, "hydration", protected=True
        )
        try:
            if progress is not None and metadata.size:
                await progress(0, metadata.size)
            result = await self.backend.download(
                relative_path,
                destination,
                expected_etag=metadata.etag,
                progress=progress,
            )
            self.cache.record_size(cache_key)
            return {
                "cache_key": cache_key,
                "cache_path": str(destination),
                "size": destination.stat().st_size,
                "etag": result.etag,
                "mtime": result.mtime,
            }
        except BaseException as exc:
            self.cache.remove(cache_key, force=True)
            self.states.transition(
                relative_path, SyncState.REMOTE_ONLY, error=str(exc), force=True
            )
            raise

    def hydration_completed(self, relative_path: str, cache_key: str) -> None:
        path = local_path(self.sync_root, relative_path)
        stat = path.stat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE files SET hydration_state='HYDRATED', local_size=?,
                    local_mtime_ns=?, last_sync_at=?, updated_at=? WHERE path_key=?
                """,
                (stat.st_size, stat.st_mtime_ns, utc_now(), utc_now(), key(relative_path)),
            )
            row = connection.execute(
                "SELECT sync_state FROM files WHERE path_key=?", (key(relative_path),)
            ).fetchone()
        if row and row["sync_state"] == SyncState.HYDRATING:
            self.states.transition(relative_path, SyncState.SYNCED)
        self.cache.protect(cache_key, False)

    async def local_modified(self, relative_path: str) -> int:
        relative_path = normalize_relative_path(relative_path)
        self.operations.cancel_pending(
            OperationType.DELETE, relative_path, "Local path was restored during delete grace"
        )
        self._cancel_remote_deletes_for_local_change(relative_path)
        path = local_path(self.sync_root, relative_path)
        for attempt in range(3):
            before = path.stat()
            cache_key, staged = await asyncio.to_thread(
                self.cache.stage_upload, path, relative_path
            )
            stat = path.stat()
            if (before.st_size, before.st_mtime_ns) == (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                break
            self.cache.remove(cache_key, force=True)
            if attempt < 2:
                await asyncio.sleep(0.2)
        else:
            raise OSError(f"File kept changing while upload staging was created: {path}")
        now = utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT remote_etag, base_etag FROM files WHERE path_key=?",
                (key(relative_path),),
            ).fetchone()
            base_etag = existing["base_etag"] if existing else None
            remote_etag = existing["remote_etag"] if existing else None
            connection.execute(
                """
                INSERT INTO files(
                    relative_path, path_key, remote_path, remote_etag, base_etag,
                    local_size, local_mtime_ns, sync_state, hydration_state,
                    dirty, is_directory, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'LOCAL_DIRTY', 'HYDRATED', 1, 0, ?)
                ON CONFLICT(path_key) DO UPDATE SET
                    local_size=excluded.local_size, local_mtime_ns=excluded.local_mtime_ns,
                    is_directory=0, tombstone=0,
                    last_error=NULL, updated_at=excluded.updated_at
                """,
                (
                    relative_path,
                    key(relative_path),
                    relative_path,
                    remote_etag,
                    base_etag,
                    stat.st_size,
                    stat.st_mtime_ns,
                    now,
                ),
            )
            file_id = int(
                connection.execute(
                    "SELECT id FROM files WHERE path_key=?", (key(relative_path),)
                ).fetchone()[0]
            )
        self.states.transition(relative_path, SyncState.LOCAL_DIRTY, force=True)
        superseded_cache_key = None
        superseded_operation_id = None
        with self.database.connect() as connection:
            pending = connection.execute(
                """
                SELECT id, payload_json FROM operations
                WHERE type='UPLOAD' AND path=? AND status='PENDING'
                """,
                (relative_path,),
            ).fetchone()
        if pending:
            superseded_operation_id = int(pending["id"])
            superseded_cache_key = json.loads(pending["payload_json"]).get("cache_key")
        operation_id = self.operations.enqueue(
            OperationType.UPLOAD,
            relative_path,
            payload={
                "cache_key": cache_key,
                "cache_path": str(staged),
                "file_id": file_id,
                "local_size": stat.st_size,
                "local_mtime_ns": stat.st_mtime_ns,
            },
            delay_seconds=self.writeback_delay_seconds,
            debounce=True,
        )
        if (
            superseded_operation_id == operation_id
            and superseded_cache_key
            and superseded_cache_key != cache_key
        ):
            self.cache.remove(str(superseded_cache_key), force=True)
        self.states.transition(relative_path, SyncState.UPLOAD_PENDING)
        if self.platform:
            try:
                await self.platform.set_in_sync(relative_path, False)
            except Exception:
                logger.exception(
                    "Unable to update Shell state for %s; durable upload remains queued",
                    relative_path,
                )
        return operation_id

    async def local_created(self, relative_path: str, *, is_directory: bool) -> int:
        if not is_directory:
            return await self.local_modified(relative_path)
        relative_path = normalize_relative_path(relative_path)
        self.operations.cancel_pending(
            OperationType.DELETE, relative_path, "Local directory was restored during delete grace"
        )
        self._cancel_remote_deletes_for_local_change(relative_path)
        path = local_path(self.sync_root, relative_path)
        if not path.is_dir():
            raise NotADirectoryError(path)
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO files(
                    relative_path, path_key, remote_path, sync_state,
                    hydration_state, dirty, is_directory, updated_at
                ) VALUES (?, ?, ?, 'LOCAL_DIRTY', 'HYDRATED', 1, 1, ?)
                ON CONFLICT(path_key) DO UPDATE SET
                    is_directory=1, tombstone=0, last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (relative_path, key(relative_path), relative_path, now),
            )
        self.states.transition(relative_path, SyncState.LOCAL_DIRTY, force=True)
        operation_id = self.operations.enqueue(
            OperationType.MKDIR,
            relative_path,
            delay_seconds=self.writeback_delay_seconds,
            debounce=True,
        )
        self.states.transition(relative_path, SyncState.UPLOAD_PENDING)
        if self.platform:
            try:
                await self.platform.set_in_sync(relative_path, False)
            except Exception:
                logger.exception(
                    "Unable to update Shell state for directory %s; MKCOL remains queued",
                    relative_path,
                )
        return operation_id

    async def local_renamed(self, source: str, destination: str) -> int:
        source = normalize_relative_path(source)
        destination = normalize_relative_path(destination)
        self.operations.cancel_pending(
            OperationType.DELETE,
            destination,
            "Local path was restored by rename during delete grace",
        )
        now = utc_now()
        local_only_operation_id = 0
        with self.database.transaction() as connection:
            existing_operation = connection.execute(
                """
                SELECT id FROM operations
                WHERE type='MOVE' AND path=? AND destination_path=?
                    AND status IN ('PENDING', 'RUNNING', 'COMPLETE')
                ORDER BY id DESC LIMIT 1
                """,
                (source, destination),
            ).fetchone()
            if existing_operation:
                return int(existing_operation[0])
            source_row = connection.execute(
                """
                SELECT id, is_directory, remote_etag, remote_size, remote_mtime,
                    last_sync_at, hydration_state
                FROM files WHERE path_key=?
                """,
                (key(source),),
            ).fetchone()
            destination_row = connection.execute(
                "SELECT id, base_etag FROM files WHERE path_key=?", (key(destination),)
            ).fetchone()
            if source_row is None:
                # A duplicate notification arrived after the first one was committed.
                if destination_row:
                    return 0
                raise KeyError(source)
            if destination_row:
                if destination_row["base_etag"] is not None:
                    raise ValueError("Rename destination already has synchronized history")
                connection.execute("DELETE FROM files WHERE id=?", (destination_row["id"],))
            has_remote_history = bool(
                source_row["hydration_state"] == "REMOTE_ONLY"
                or source_row["remote_etag"] is not None
                or source_row["remote_size"] is not None
                or source_row["remote_mtime"] is not None
                or source_row["last_sync_at"] is not None
            )
            if source_row["is_directory"]:
                all_files = connection.execute(
                    "SELECT id, relative_path, path_key FROM files"
                ).fetchall()
                source_prefix = source.casefold() + "/"
                subtree = [
                    row
                    for row in all_files
                    if row["path_key"] == key(source)
                    or row["path_key"].startswith(source_prefix)
                ]
                subtree_ids = {int(row["id"]) for row in subtree}
                occupied = {
                    row["path_key"]: int(row["id"])
                    for row in all_files
                    if int(row["id"]) not in subtree_ids
                }
                remapped = [
                    (
                        int(row["id"]),
                        destination + str(row["relative_path"])[len(source) :],
                    )
                    for row in subtree
                ]
                collision = next(
                    (path for _, path in remapped if key(path) in occupied), None
                )
                if collision:
                    raise ValueError(f"Rename subtree destination already exists: {collision}")
                for file_id, new_path in remapped:
                    connection.execute(
                        """
                        UPDATE files SET relative_path=?, path_key=?, remote_path=?,
                            updated_at=? WHERE id=?
                        """,
                        (new_path, key(new_path), new_path, now, file_id),
                    )
                pending_operations = connection.execute(
                    """
                    SELECT id, path, destination_path FROM operations
                    WHERE status='PENDING'
                    """
                ).fetchall()
                for pending in pending_operations:
                    operation_path = self._renamed_child(
                        str(pending["path"]), source, destination
                    )
                    operation_destination = (
                        self._renamed_child(
                            str(pending["destination_path"]), source, destination
                        )
                        if pending["destination_path"]
                        else None
                    )
                    connection.execute(
                        "UPDATE operations SET path=?, destination_path=?, updated_at=? "
                        "WHERE id=?",
                        (
                            operation_path,
                            operation_destination,
                            now,
                            pending["id"],
                        ),
                    )
            else:
                connection.execute(
                    """
                    UPDATE files SET relative_path=?, path_key=?, remote_path=?,
                        updated_at=? WHERE path_key=?
                    """,
                    (destination, key(destination), destination, now, key(source)),
                )
                if not has_remote_history:
                    pending_operations = connection.execute(
                        """
                        SELECT id, path, destination_path FROM operations
                        WHERE status='PENDING'
                        """
                    ).fetchall()
                    for pending in pending_operations:
                        operation_path = self._renamed_child(
                            str(pending["path"]), source, destination
                        )
                        operation_destination = (
                            self._renamed_child(
                                str(pending["destination_path"]), source, destination
                            )
                            if pending["destination_path"]
                            else None
                        )
                        connection.execute(
                            "UPDATE operations SET path=?, destination_path=?, updated_at=? "
                            "WHERE id=?",
                            (
                                operation_path,
                                operation_destination,
                                now,
                                pending["id"],
                            ),
                        )
            if not has_remote_history:
                local_operation = connection.execute(
                    """
                    SELECT id FROM operations WHERE path=? AND status='PENDING'
                        AND type IN ('UPLOAD', 'MKDIR')
                    ORDER BY id LIMIT 1
                    """,
                    (destination,),
                ).fetchone()
                local_only_operation_id = int(local_operation["id"]) if local_operation else 0
        if has_remote_history:
            self.states.transition(destination, SyncState.LOCAL_DIRTY, force=True)
            operation_id = self.operations.enqueue(
                OperationType.MOVE,
                source,
                destination_path=destination,
                delay_seconds=self.writeback_delay_seconds,
                debounce=True,
            )
        else:
            self.states.transition(destination, SyncState.UPLOAD_PENDING, force=True)
            operation_id = local_only_operation_id
        if self.platform:
            try:
                await self.platform.set_in_sync(destination, False)
            except Exception:
                logger.exception(
                    "Unable to update Shell state for renamed path %s; MOVE remains queued",
                    destination,
                )
        return operation_id

    async def local_deleted(self, relative_path: str) -> int:
        relative_path = normalize_relative_path(relative_path)
        with self.database.connect() as connection:
            moved = connection.execute(
                """
                SELECT id FROM operations WHERE type='MOVE' AND path=?
                    AND status IN ('PENDING', 'RUNNING', 'COMPLETE')
                ORDER BY id DESC LIMIT 1
                """,
                (relative_path,),
            ).fetchone()
            active_deletes = connection.execute(
                """
                SELECT id, path FROM operations WHERE type='DELETE'
                    AND status IN ('PENDING', 'RUNNING')
                """
            ).fetchall()
            row = connection.execute(
                "SELECT id, is_directory FROM files WHERE path_key=?",
                (key(relative_path),),
            ).fetchone()
        if moved:
            return int(moved[0])
        ancestor_delete = next(
            (
                operation
                for operation in active_deletes
                if relative_path.casefold() == operation["path"].casefold()
                or relative_path.casefold().startswith(
                    operation["path"].casefold() + "/"
                )
            ),
            None,
        )
        if ancestor_delete:
            return int(ancestor_delete["id"])
        if row is None:
            return 0
        paths = [relative_path]
        if row["is_directory"]:
            prefix = relative_path.casefold() + "/"
            with self.database.connect() as connection:
                paths.extend(
                    str(child["relative_path"])
                    for child in connection.execute(
                        "SELECT relative_path, path_key FROM files"
                    )
                    if child["path_key"].startswith(prefix)
                )
        for path in paths:
            self.states.transition(path, SyncState.DELETE_PENDING, force=True)
        with self.database.transaction() as connection:
            pending = connection.execute(
                """
                SELECT id, path FROM operations WHERE status='PENDING'
                    AND type <> 'DELETE'
                """
            ).fetchall()
            for operation in pending:
                if operation["path"].casefold() == relative_path.casefold() or operation[
                    "path"
                ].casefold().startswith(relative_path.casefold() + "/"):
                    connection.execute(
                        """
                        UPDATE operations SET status='CANCELLED', updated_at=?,
                            last_error='Superseded by local directory delete'
                        WHERE id=?
                        """,
                        (utc_now(), operation["id"]),
                    )
        return self.operations.enqueue(
            OperationType.DELETE,
            relative_path,
            delay_seconds=self.delete_grace_seconds,
            debounce=True,
        )

    async def process_one(self, operation_type: OperationType | None = None) -> bool:
        operation = self.operations.claim_due(operation_type)
        if operation is None:
            return False
        try:
            if operation.type is OperationType.UPLOAD:
                await self._upload(operation)
            elif operation.type is OperationType.DELETE:
                await self._delete(operation)
            elif operation.type is OperationType.MOVE:
                await self._move(operation)
            elif operation.type is OperationType.MKDIR:
                await self._mkdir(operation)
            elif operation.type is OperationType.DOWNLOAD:
                await self._remote_update(operation)
            elif operation.type is OperationType.REMOTE_DELETE:
                await self._remote_delete(operation)
            self.operations.complete(operation.id)
            return True
        except OperationSuperseded as exc:
            self.operations.cancel(operation.id, str(exc))
            logger.info("Operation %d was superseded: %s", operation.id, exc)
            return True
        except BackendError as exc:
            delay = self.operations.retry(operation.id, str(exc))
            if operation.type is OperationType.UPLOAD:
                self._transition_operation(
                    operation, SyncState.UPLOAD_PENDING, force=True
                )
            elif operation.type in {OperationType.DOWNLOAD, OperationType.REMOTE_DELETE}:
                self._transition_operation(
                    operation, SyncState.DOWNLOAD_PENDING, force=True
                )
            logger.warning("Operation %d will retry in %.1fs: %s", operation.id, delay, exc)
            return True
        except ValueError as exc:
            self.operations.fail(operation.id, str(exc))
            logger.error("Operation %d failed permanently: %s", operation.id, exc)
            return True
        except OSError as exc:
            delay = self.operations.retry(operation.id, str(exc))
            if operation.type is OperationType.UPLOAD:
                self._transition_operation(
                    operation, SyncState.UPLOAD_PENDING, force=True
                )
            logger.warning(
                "Local I/O for operation %d will retry in %.1fs: %s",
                operation.id,
                delay,
                exc,
            )
            return True
        except Exception as exc:
            # Backend implementations must normally raise BackendError, but an adapter or
            # transport bug must still never drop a durable dirty operation or kill workers.
            delay = self.operations.retry(operation.id, str(exc))
            logger.exception(
                "Unexpected operation error; operation %d will retry in %.1fs",
                operation.id,
                delay,
            )
            return True

    async def _upload(self, operation: Operation) -> None:
        relative_path = operation.path
        source = Path(str(operation.payload["cache_path"]))
        with self.database.connect() as connection:
            active_moves = connection.execute(
                """
                SELECT id, destination_path FROM operations
                WHERE type='MOVE' AND status IN ('PENDING', 'RUNNING') AND id<>?
                """,
                (operation.id,),
            ).fetchall()
            predecessor = next(
                (
                    move
                    for move in active_moves
                    if move["destination_path"]
                    and (
                        relative_path.casefold()
                        == str(move["destination_path"]).casefold()
                        or relative_path.casefold().startswith(
                            str(move["destination_path"]).casefold() + "/"
                        )
                    )
                ),
                None,
            )
            if predecessor:
                raise BackendError(
                    f"Rename operation {predecessor[0]} must complete before upload"
                )
            file_id = operation.payload.get("file_id")
            if file_id is not None:
                row = connection.execute(
                    "SELECT base_etag, relative_path FROM files WHERE id=?", (file_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT base_etag, relative_path FROM files WHERE path_key=?",
                    (key(relative_path),),
                ).fetchone()
        base_etag = row[0] if row else None
        state_path = str(row["relative_path"]) if row else relative_path
        try:
            remote = await self.backend.stat(relative_path)
        except RemoteNotFoundError:
            remote = None
        remote_etag = remote.etag if remote else None
        if (base_etag is None and remote is not None) or (
            base_etag is not None and remote_etag != base_etag
        ):
            if file_id is not None:
                with self.database.connect() as connection:
                    current = connection.execute(
                        "SELECT relative_path FROM files WHERE id=?", (file_id,)
                    ).fetchone()
                if current:
                    state_path = str(current["relative_path"])
            self._preserve_local_conflict(state_path, remote_etag, base_etag)
            self.states.transition(state_path, SyncState.CONFLICT, force=True)
            raise ValueError("Conflict: remote version changed since the local base")
        self.states.transition(state_path, SyncState.UPLOADING, force=True)
        result = await self.backend.upload(source, relative_path, expected_etag=base_etag)
        now = utc_now()
        with self.database.transaction() as connection:
            file_id = operation.payload.get("file_id")
            values = (
                result.etag,
                result.etag,
                result.size,
                result.mtime,
                now,
                now,
            )
            if file_id is not None:
                connection.execute(
                    """
                    UPDATE files SET remote_etag=?, base_etag=?, remote_size=?,
                        remote_mtime=?, last_sync_at=?, updated_at=? WHERE id=?
                    """,
                    (*values, file_id),
                )
                row = connection.execute(
                    "SELECT relative_path, sync_state FROM files WHERE id=?", (file_id,)
                ).fetchone()
            else:
                path_key = key(relative_path)
                connection.execute(
                    """
                    UPDATE files SET remote_etag=?, base_etag=?, remote_size=?,
                        remote_mtime=?, last_sync_at=?, updated_at=? WHERE path_key=?
                    """,
                    (*values, path_key),
                )
                row = connection.execute(
                    "SELECT relative_path, sync_state FROM files WHERE path_key=?",
                    (path_key,),
                ).fetchone()
        self.cache.remove(str(operation.payload["cache_key"]), force=True)
        if row is None:
            return
        current_path = str(row["relative_path"])
        if row["sync_state"] in {SyncState.DELETE_PENDING, SyncState.CONFLICT}:
            return
        with self.database.connect() as connection:
            newer_upload = connection.execute(
                """
                SELECT id FROM operations
                WHERE type='UPLOAD' AND id<>? AND status IN ('PENDING', 'RUNNING')
                    AND path IN (?, ?)
                LIMIT 1
                """,
                (operation.id, relative_path, current_path),
            ).fetchone()
            pending_move = connection.execute(
                """
                SELECT id FROM operations
                WHERE type='MOVE' AND status IN ('PENDING', 'RUNNING')
                    AND (path=? OR destination_path=?)
                LIMIT 1
                """,
                (relative_path, current_path),
            ).fetchone()
        if newer_upload or pending_move or current_path != relative_path:
            if newer_upload:
                self.states.transition(current_path, SyncState.UPLOAD_PENDING, force=True)
            if self.platform:
                await self.platform.set_in_sync(current_path, False)
            return
        current = local_path(self.sync_root, current_path)
        try:
            stat = current.stat()
        except FileNotFoundError:
            # A delete notification may still be in flight. Never report an absent file as synced.
            return
        snapshot = (
            operation.payload.get("local_size"),
            operation.payload.get("local_mtime_ns"),
        )
        if None not in snapshot and snapshot != (stat.st_size, stat.st_mtime_ns):
            await self.local_modified(current_path)
            return
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE files SET local_size=?, local_mtime_ns=? WHERE path_key=?",
                (stat.st_size, stat.st_mtime_ns, key(current_path)),
            )
        self.states.transition(current_path, SyncState.SYNCED, force=True)
        if self.platform:
            await self.platform.set_in_sync(current_path, True)

    async def _delete(self, operation: Operation) -> None:
        with self.database.connect() as connection:
            active_uploads = connection.execute(
                """
                SELECT id, path FROM operations WHERE type='UPLOAD'
                    AND status IN ('PENDING', 'RUNNING')
                """
            ).fetchall()
            row = connection.execute(
                "SELECT base_etag, sync_state, is_directory FROM files WHERE path_key=?",
                (key(operation.path),),
            ).fetchone()
        if row is None:
            return
        if row["sync_state"] != SyncState.DELETE_PENDING or local_path(
            self.sync_root, operation.path
        ).exists():
            raise OperationSuperseded("Local path no longer has an active delete tombstone")
        upload_prefix = operation.path.casefold() + "/"
        active_upload = next(
            (
                upload
                for upload in active_uploads
                if upload["path"].casefold() == operation.path.casefold()
                or upload["path"].casefold().startswith(upload_prefix)
            ),
            None,
        )
        if active_upload:
            raise BackendError(
                f"Upload operation {active_upload[0]} must complete before delete"
            )
        base_etag = row["base_etag"]
        try:
            remote = await self.backend.stat(operation.path)
        except RemoteNotFoundError:
            remote = None
        if remote and base_etag and remote.etag != base_etag:
            self._preserve_local_conflict(operation.path, remote.etag, base_etag)
            self.states.transition(operation.path, SyncState.CONFLICT, force=True)
            raise ValueError("Conflict: remote changed during delete grace period")
        if remote:
            await self.backend.delete(operation.path)
        removed_paths = [operation.path]
        with self.database.transaction() as connection:
            if row["is_directory"]:
                all_files = connection.execute(
                    "SELECT id, relative_path, path_key FROM files"
                ).fetchall()
                removed = [
                    child
                    for child in all_files
                    if child["path_key"] == key(operation.path)
                    or child["path_key"].startswith(upload_prefix)
                ]
                removed_paths = [str(child["relative_path"]) for child in removed]
                connection.executemany(
                    "DELETE FROM files WHERE id=?",
                    [(child["id"],) for child in removed],
                )
                for queued in connection.execute(
                    """
                    SELECT id, path FROM operations WHERE id<>?
                        AND status IN ('PENDING', 'RUNNING')
                    """,
                    (operation.id,),
                ).fetchall():
                    if queued["path"].casefold().startswith(upload_prefix):
                        connection.execute(
                            """
                            UPDATE operations SET status='CANCELLED', updated_at=?,
                                last_error='Superseded by completed directory delete'
                            WHERE id=?
                            """,
                            (utc_now(), queued["id"]),
                        )
            else:
                connection.execute(
                    "DELETE FROM files WHERE path_key=?", (key(operation.path),)
                )
        for removed_path in removed_paths:
            self.cache.remove_owner(removed_path, force=True)

    async def _move(self, operation: Operation) -> None:
        if not operation.destination_path:
            raise ValueError("MOVE operation has no destination")
        with self.database.connect() as connection:
            active_uploads = connection.execute(
                """
                SELECT id, path FROM operations WHERE type='UPLOAD'
                    AND status IN ('PENDING', 'RUNNING')
                """,
            ).fetchall()
            row = connection.execute(
                "SELECT base_etag, is_directory FROM files WHERE path_key=?",
                (key(operation.destination_path),),
            ).fetchone()
        source_prefix = operation.path.casefold() + "/"
        active_upload = next(
            (
                upload
                for upload in active_uploads
                if upload["path"].casefold() == operation.path.casefold()
                or upload["path"].casefold().startswith(source_prefix)
            ),
            None,
        )
        if active_upload:
            raise BackendError(
                f"Upload operation {active_upload[0]} must complete before move"
            )
        base_etag = row["base_etag"] if row else None
        try:
            source_metadata = await self.backend.stat(operation.path)
        except RemoteNotFoundError:
            self._preserve_local_conflict(
                operation.destination_path, None, base_etag
            )
            self.states.transition(
                operation.destination_path, SyncState.CONFLICT, force=True
            )
            raise ValueError("Conflict: remote source disappeared before rename") from None
        if base_etag and source_metadata.etag and source_metadata.etag != base_etag:
            self._preserve_local_conflict(
                operation.destination_path, source_metadata.etag, base_etag
            )
            self.states.transition(operation.destination_path, SyncState.CONFLICT, force=True)
            raise ValueError("Conflict: remote source changed before rename")
        try:
            destination_metadata = await self.backend.stat(operation.destination_path)
        except RemoteNotFoundError:
            pass
        else:
            self._preserve_local_conflict(
                operation.destination_path, destination_metadata.etag, base_etag
            )
            self.states.transition(
                operation.destination_path, SyncState.CONFLICT, force=True
            )
            raise ValueError("Conflict: rename destination already exists remotely")
        await self.backend.move(operation.path, operation.destination_path)
        metadata = await self.backend.stat(operation.destination_path)
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE files SET remote_etag=?, base_etag=?, remote_size=?, remote_mtime=?,
                    last_sync_at=?, updated_at=?
                WHERE path_key=?
                """,
                (
                    metadata.etag,
                    metadata.etag,
                    metadata.size,
                    metadata.mtime,
                    utc_now(),
                    utc_now(),
                    key(operation.destination_path),
                ),
            )
            newer_upload = connection.execute(
                """
                SELECT id FROM operations WHERE type='UPLOAD' AND path=?
                    AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """,
                (operation.destination_path,),
            ).fetchone()
        if newer_upload:
            self.states.transition(
                operation.destination_path, SyncState.UPLOAD_PENDING, force=True
            )
        else:
            self.states.transition(operation.destination_path, SyncState.SYNCED, force=True)
        if self.platform:
            await self.platform.set_in_sync(
                operation.destination_path, newer_upload is None
            )

    async def _mkdir(self, operation: Operation) -> None:
        await self.backend.mkdir(operation.path)
        metadata = await self.backend.stat(operation.path)
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE files SET remote_etag=?, base_etag=?, remote_mtime=?,
                    is_directory=1, last_sync_at=?, updated_at=? WHERE path_key=?
                """,
                (metadata.etag, metadata.etag, metadata.mtime, now, now, key(operation.path)),
            )
        self.states.transition(operation.path, SyncState.SYNCED, force=True)
        if self.platform:
            await self.platform.set_in_sync(operation.path, True)

    async def _remote_update(self, operation: Operation) -> None:
        relative_path = operation.path
        metadata = await self.backend.stat(relative_path)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT dirty, base_etag FROM files WHERE path_key=?",
                (key(relative_path),),
            ).fetchone()
        if row and row["dirty"]:
            if row["base_etag"] != metadata.etag:
                self._preserve_local_conflict(
                    relative_path, metadata.etag, row["base_etag"]
                )
                self.states.transition(relative_path, SyncState.CONFLICT, force=True)
                raise ValueError("Conflict: local content changed before remote update")
            raise OperationSuperseded("Remote version returned to the local base")
        self._ensure_file_row(metadata)
        if self.platform:
            await self.platform.update_placeholder(self._placeholder(metadata))
        with self.database.connect() as connection:
            after_platform = connection.execute(
                "SELECT dirty FROM files WHERE path_key=?", (key(relative_path),)
            ).fetchone()
        if after_platform and after_platform["dirty"]:
            # The local edit happened after the clean remote version was applied. It is now
            # based on this ETag and must remain queued instead of being marked REMOTE_ONLY.
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE files SET remote_etag=?, base_etag=?, remote_size=?,
                        remote_mtime=?, updated_at=? WHERE path_key=?
                    """,
                    (
                        metadata.etag,
                        metadata.etag,
                        metadata.size,
                        metadata.mtime,
                        utc_now(),
                        key(relative_path),
                    ),
                )
            return
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE files SET base_etag=?, hydration_state=?, last_sync_at=?,
                    updated_at=? WHERE path_key=?
                """,
                (
                    metadata.etag,
                    "HYDRATED" if metadata.is_dir else "REMOTE_ONLY",
                    utc_now(),
                    utc_now(),
                    key(relative_path),
                ),
            )
        self.states.transition(
            relative_path,
            SyncState.SYNCED if metadata.is_dir else SyncState.REMOTE_ONLY,
            force=True,
        )

    async def _remote_delete(self, operation: Operation) -> None:
        relative_path = operation.path
        with self.database.connect() as connection:
            operation_row = connection.execute(
                "SELECT status FROM operations WHERE id=?", (operation.id,)
            ).fetchone()
            row = connection.execute(
                "SELECT dirty, sync_state FROM files WHERE path_key=?",
                (key(relative_path),),
            ).fetchone()
        if operation_row is None or operation_row["status"] != "RUNNING":
            raise OperationSuperseded("Remote delete was cancelled by a local change")
        if row is None:
            return
        if row["dirty"] or row["sync_state"] != SyncState.DOWNLOAD_PENDING:
            raise OperationSuperseded("Local content changed before remote delete")
        if self.platform:
            await self.platform.remove_placeholder(relative_path)
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM files WHERE path_key=?", (key(relative_path),))

    async def poll_remote_directory(self, directory: str = "") -> tuple[int, int]:
        changed, deleted = await self.remote_scanner.poll_directory(directory)
        for entry in changed:
            with self.database.connect() as connection:
                local = connection.execute(
                    "SELECT dirty, base_etag FROM files WHERE path_key=?", (key(entry.path),)
                ).fetchone()
            if local and local["dirty"] and local["base_etag"] != entry.etag:
                self._preserve_local_conflict(
                    entry.path, entry.etag, local["base_etag"]
                )
                self.states.transition(entry.path, SyncState.CONFLICT, force=True)
                continue
            if local and local["base_etag"] == entry.etag:
                continue
            self._ensure_file_row(entry)
            self.states.transition(entry.path, SyncState.DOWNLOAD_PENDING, force=True)
            self.operations.enqueue(
                OperationType.DOWNLOAD, entry.path, payload={"etag": entry.etag}, debounce=True
            )
        for path in deleted:
            with self.database.connect() as connection:
                local = connection.execute(
                    "SELECT dirty FROM files WHERE path_key=?", (key(path),)
                ).fetchone()
            if local and local["dirty"]:
                self.states.transition(path, SyncState.CONFLICT, force=True)
            else:
                if local:
                    self.states.transition(path, SyncState.DOWNLOAD_PENDING, force=True)
                    self.operations.enqueue(
                        OperationType.REMOTE_DELETE, path, debounce=True
                    )
        return len(changed), len(deleted)

    def _preserve_local_conflict(
        self, relative_path: str, remote_etag: str | None, base_etag: str | None
    ) -> None:
        path = local_path(self.sync_root, relative_path)
        if path.is_file():
            self.conflicts.keep_local_copy(path, relative_path, remote_etag, base_etag)
            return
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO conflicts(
                    path, local_path, remote_etag, base_etag, status, created_at, details
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    relative_path,
                    str(path),
                    remote_etag,
                    base_etag,
                    utc_now(),
                    "Local/remote metadata conflict; no regular local file was copied",
                ),
            )

    def _transition_operation(
        self, operation: Operation, target: SyncState, *, force: bool = False
    ) -> None:
        with self.database.connect() as connection:
            file_id = operation.payload.get("file_id")
            if file_id is not None:
                row = connection.execute(
                    "SELECT relative_path FROM files WHERE id=?", (file_id,)
                ).fetchone()
            else:
                candidate = operation.destination_path or operation.path
                row = connection.execute(
                    "SELECT relative_path FROM files WHERE path_key=?", (key(candidate),)
                ).fetchone()
        if row:
            self.states.transition(str(row["relative_path"]), target, force=force)

    def _cancel_remote_deletes_for_local_change(self, relative_path: str) -> None:
        with self.database.transaction() as connection:
            operations = connection.execute(
                """
                SELECT id, path FROM operations WHERE type='REMOTE_DELETE'
                    AND status IN ('PENDING', 'RUNNING')
                """
            ).fetchall()
            ancestors = [
                str(operation["path"])
                for operation in operations
                if relative_path.casefold() == operation["path"].casefold()
                or relative_path.casefold().startswith(
                    operation["path"].casefold() + "/"
                )
            ]
            for operation in operations:
                if str(operation["path"]) in ancestors:
                    connection.execute(
                        """
                        UPDATE operations SET status='CANCELLED', updated_at=?,
                            last_error='Cancelled because local content changed'
                        WHERE id=?
                        """,
                        (utc_now(), operation["id"]),
                    )
        for ancestor in ancestors:
            if ancestor.casefold() == relative_path.casefold():
                continue
            try:
                self._preserve_local_conflict(ancestor, None, None)
                self.states.transition(ancestor, SyncState.CONFLICT, force=True)
            except KeyError:
                pass

    @staticmethod
    def _renamed_child(path: str, source: str, destination: str) -> str:
        if path.casefold() == source.casefold():
            return destination
        if path.casefold().startswith(source.casefold() + "/"):
            return destination + path[len(source) :]
        return path

    def _ensure_file_row(self, entry: RemoteEntry) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO files(
                    relative_path, path_key, remote_path, remote_etag, remote_size,
                    remote_mtime, sync_state, hydration_state, base_etag,
                    is_directory, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'REMOTE_ONLY', 'REMOTE_ONLY', ?, ?, ?)
                ON CONFLICT(path_key) DO UPDATE SET remote_etag=excluded.remote_etag,
                    remote_size=excluded.remote_size, remote_mtime=excluded.remote_mtime,
                    is_directory=excluded.is_directory,
                    updated_at=excluded.updated_at
                """,
                (
                    entry.path,
                    key(entry.path),
                    entry.path,
                    entry.etag,
                    entry.size,
                    entry.mtime,
                    entry.etag,
                    int(entry.is_dir),
                    utc_now(),
                ),
            )

    @staticmethod
    def _placeholder(entry: RemoteEntry) -> Placeholder:
        return Placeholder(
            relative_path=entry.path,
            is_directory=entry.is_dir,
            size=entry.size,
            mtime=entry.mtime,
            etag=entry.etag,
        )
