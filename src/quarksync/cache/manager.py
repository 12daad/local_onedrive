from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from quarksync.db.database import Database, utc_now


class CachePressureError(RuntimeError):
    pass


class CacheManager:
    def __init__(
        self,
        root: Path,
        database: Database,
        *,
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
        self.root = root
        self.database = database
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(value: str, purpose: str) -> str:
        return hashlib.sha256(f"{purpose}\0{value}".encode()).hexdigest()

    def allocate(self, value: str, purpose: str, *, protected: bool) -> tuple[str, Path]:
        cache_key = self._key(value + uuid.uuid4().hex, purpose)
        directory = self.root / purpose
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / cache_key
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries(
                    cache_key, path, purpose, size, protected, created_at, last_access_at,
                    owner_key
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (cache_key, str(path), purpose, int(protected), now, now, value.casefold()),
            )
        return cache_key, path

    def stage_upload(self, source: Path, relative_path: str) -> tuple[str, Path]:
        cache_key, destination = self.allocate(relative_path, "uploads", protected=True)
        try:
            with source.open("rb", buffering=0) as input_file, destination.open(
                "xb", buffering=0
            ) as output_file:
                while chunk := input_file.read(1024 * 1024):
                    output_file.write(chunk)
                os.fsync(output_file.fileno())
            self.record_size(cache_key)
            return cache_key, destination
        except BaseException:
            self.remove(cache_key, force=True)
            raise

    def record_size(self, cache_key: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT path FROM cache_entries WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                raise KeyError(cache_key)
            size = Path(row[0]).stat().st_size
            connection.execute(
                "UPDATE cache_entries SET size = ?, last_access_at = ? WHERE cache_key = ?",
                (size, utc_now(), cache_key),
            )

    def protect(self, cache_key: str, protected: bool = True) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cache_entries SET protected = ?, last_access_at = ? WHERE cache_key = ?",
                (int(protected), utc_now(), cache_key),
            )

    def remove(self, cache_key: str, *, force: bool = False) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT path, protected FROM cache_entries WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return
            if row[1] and not force:
                raise PermissionError("Protected cache data cannot be evicted")
            Path(row[0]).unlink(missing_ok=True)
            connection.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))

    def remove_owner(self, owner: str, *, force: bool = False) -> int:
        with self.database.connect() as connection:
            keys = [
                row[0]
                for row in connection.execute(
                    "SELECT cache_key FROM cache_entries WHERE owner_key = ?", (owner.casefold(),)
                )
            ]
        for cache_key in keys:
            self.remove(cache_key, force=force)
        return len(keys)

    def recover_transient_hydrations(self) -> int:
        """Discard restart-orphaned downloads; upload staging is never touched."""
        with self.database.connect() as connection:
            keys = [
                row[0]
                for row in connection.execute(
                    "SELECT cache_key FROM cache_entries WHERE purpose='hydration'"
                )
            ]
        for cache_key in keys:
            try:
                self.remove(cache_key, force=True)
            except OSError:
                # A surviving provider may still hold the completed cache file while a
                # replacement daemon adopts it after a crash. Keep it protected for now.
                continue
        with self.database.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM cache_entries WHERE purpose='hydration'"
            ).fetchone()[0]
        return len(keys) - int(remaining)

    def evict(self) -> int:
        removed = 0
        with self.database.connect() as connection:
            total = int(
                connection.execute("SELECT COALESCE(SUM(size), 0) FROM cache_entries").fetchone()[0]
            )
            candidates = connection.execute(
                "SELECT cache_key, path, size FROM cache_entries "
                "WHERE protected = 0 ORDER BY last_access_at"
            ).fetchall()
        free = shutil.disk_usage(self.root).free
        for cache_key, _, size in candidates:
            if total <= self.max_bytes and free >= self.min_free_bytes:
                break
            self.remove(cache_key)
            total -= size
            free += size
            removed += 1
        if total > self.max_bytes or free < self.min_free_bytes:
            raise CachePressureError("Cache limits cannot be met without deleting protected data")
        return removed
