from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable

from quarksync.backend.base import RemoteEntry, StorageBackend
from quarksync.db.database import Database, utc_now
from quarksync.status.devices import META_ROOT
from quarksync.sync.paths import key, validate_windows_relative_path

logger = logging.getLogger(__name__)


class RemoteScanner:
    def __init__(self, database: Database, backend: StorageBackend) -> None:
        self.database = database
        self.backend = backend

    async def eager_enumerate(
        self,
        *,
        batch_size: int = 500,
        cancelled: Callable[[], bool] | None = None,
        on_batch: Callable[[list[RemoteEntry]], Awaitable[None]] | None = None,
    ) -> int:
        queue = deque([""])
        count = 0
        generation = self._next_generation()
        batch: list[RemoteEntry] = []
        while queue:
            if cancelled and cancelled():
                break
            directory = queue.popleft()
            entries = await self.backend.list_dir(directory)
            for entry in entries:
                if entry.path == META_ROOT or entry.path.startswith(META_ROOT + "/"):
                    continue
                try:
                    validate_windows_relative_path(entry.path)
                except ValueError as exc:
                    logger.error("Remote path cannot map to Windows: %s (%s)", entry.path, exc)
                    self._record_namespace_conflict(entry.path, str(exc))
                    continue
                if not self._save(entry, generation):
                    continue
                batch.append(entry)
                count += 1
                if entry.is_dir:
                    queue.append(entry.path)
                if len(batch) >= batch_size:
                    if on_batch:
                        await on_batch(batch)
                    logger.info("Enumerated %d remote entries", count)
                    batch = []
            self._mark_directory_scanned(directory, generation)
        if batch and on_batch:
            await on_batch(batch)
        return count

    async def poll_directory(self, directory: str) -> tuple[list[RemoteEntry], list[str]]:
        entries = await self.backend.list_dir(directory)
        generation = self._next_generation()
        remote_keys = set()
        changes: list[RemoteEntry] = []
        for entry in entries:
            if entry.path == META_ROOT or entry.path.startswith(META_ROOT + "/"):
                continue
            entry_key = key(entry.path)
            remote_keys.add(entry_key)
            previous = self._get(entry_key)
            if not self._save(entry, generation):
                continue
            if previous is None or (
                previous["etag"], previous["size"], previous["mtime"]
            ) != (entry.etag, entry.size, entry.mtime):
                changes.append(entry)
        prefix = key(directory)
        with self.database.connect() as connection:
            known = connection.execute(
                "SELECT path_key, relative_path, is_dir FROM remote_snapshot "
                "WHERE parent_path_key = ?",
                (prefix,),
            ).fetchall()
            namespace = connection.execute(
                "SELECT path_key, relative_path FROM remote_snapshot"
            ).fetchall()
        missing = [row for row in known if row["path_key"] not in remote_keys]
        deleted_by_key = {row["path_key"]: row["relative_path"] for row in missing}
        for row in missing:
            if not row["is_dir"]:
                continue
            child_prefix = row["path_key"] + "/"
            deleted_by_key.update(
                {
                    child["path_key"]: child["relative_path"]
                    for child in namespace
                    if child["path_key"].startswith(child_prefix)
                }
            )
        deleted = sorted(
            deleted_by_key.values(), key=lambda value: value.count("/"), reverse=True
        )
        if deleted_by_key:
            with self.database.transaction() as connection:
                connection.executemany(
                    "DELETE FROM remote_snapshot WHERE path_key=?",
                    [(entry_key,) for entry_key in deleted_by_key],
                )
                connection.executemany(
                    "DELETE FROM scan_cursors WHERE path_key=?",
                    [(entry_key,) for entry_key in deleted_by_key],
                )
        self._mark_directory_scanned(directory, generation)
        return changes, deleted

    def next_directory(self) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT relative_path FROM scan_cursors
                ORDER BY COALESCE(last_scanned_at, ''), generation, path_key
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row else ""

    def namespace(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT relative_path, etag, size, mtime, is_dir FROM remote_snapshot "
                "ORDER BY relative_path"
            ).fetchall()
        return [dict(row) for row in rows]

    def _next_generation(self) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(scan_generation), 0) + 1 FROM remote_snapshot"
                ).fetchone()[0]
            )

    def _get(self, entry_key: str):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM remote_snapshot WHERE path_key = ?", (entry_key,)
            ).fetchone()

    def _save(self, entry: RemoteEntry, generation: int) -> bool:
        entry_key = key(entry.path)
        parent = entry.path.rpartition("/")[0]
        now = utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT relative_path FROM remote_snapshot WHERE path_key=?", (entry_key,)
            ).fetchone()
            if existing and existing[0] != entry.path:
                connection.execute(
                    """
                    INSERT INTO conflicts(path, remote_etag, status, created_at, details)
                    VALUES (?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        entry.path,
                        entry.etag,
                        now,
                        f"Windows case-insensitive collision with {existing[0]}",
                    ),
                )
                logger.error(
                    "Remote case collision cannot map to NTFS: %s and %s",
                    existing[0],
                    entry.path,
                )
                return False
            connection.execute(
                """
                INSERT INTO remote_snapshot(
                    path_key, relative_path, parent_path_key, etag, size, mtime,
                    is_dir, scan_generation, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path_key) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    parent_path_key=excluded.parent_path_key, etag=excluded.etag,
                    size=excluded.size, mtime=excluded.mtime, is_dir=excluded.is_dir,
                    scan_generation=excluded.scan_generation, seen_at=excluded.seen_at
                """,
                (
                    entry_key,
                    entry.path,
                    key(parent) if parent else "",
                    entry.etag,
                    entry.size,
                    entry.mtime,
                    int(entry.is_dir),
                    generation,
                    now,
                ),
            )
        return True

    def _record_namespace_conflict(self, path: str, details: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO conflicts(path, status, created_at, details)
                VALUES (?, 'OPEN', ?, ?)
                """,
                (path, utc_now(), f"Remote path cannot map safely to Windows: {details}"),
            )

    def _mark_directory_scanned(self, directory: str, generation: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scan_cursors(
                    path_key, relative_path, last_scanned_at, generation, pending
                )
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(path_key) DO UPDATE SET last_scanned_at=excluded.last_scanned_at,
                    generation=excluded.generation, pending=0
                """,
                (key(directory) if directory else "", directory, utc_now(), generation),
            )
