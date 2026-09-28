from __future__ import annotations

import os
from pathlib import Path

from quarksync.db.database import Database
from quarksync.sync.engine import SyncEngine
from quarksync.sync.paths import key


class LocalSafetyScanner:
    """Metadata-only reconciliation. It never opens placeholder file content."""

    def __init__(self, root: Path, database: Database, engine: SyncEngine) -> None:
        self.root = root
        self.database = database
        self.engine = engine

    async def scan(self) -> int:
        changed = 0
        seen: set[str] = set()
        for directory, names, files in os.walk(self.root):
            names[:] = [name for name in names if name != ".quarksync-meta"]
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                seen.add(key(relative))
                with self.database.connect() as connection:
                    row = connection.execute(
                        "SELECT id FROM files WHERE path_key=?", (key(relative),)
                    ).fetchone()
                if row is None:
                    await self.engine.local_created(relative, is_directory=True)
                    changed += 1
            for name in files:
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                seen.add(key(relative))
                # stat() reads metadata only; no content handle is opened, so it does not hydrate.
                stat = path.stat()
                with self.database.connect() as connection:
                    row = connection.execute(
                        """
                        SELECT local_size, local_mtime_ns, hydration_state
                        FROM files WHERE path_key=?
                        """,
                        (key(relative),),
                    ).fetchone()
                if row is None:
                    await self.engine.local_modified(relative)
                    changed += 1
                elif row["hydration_state"] != "REMOTE_ONLY" and (
                    row["local_size"], row["local_mtime_ns"]
                ) != (stat.st_size, stat.st_mtime_ns):
                    await self.engine.local_modified(relative)
                    changed += 1
        with self.database.connect() as connection:
            known = connection.execute(
                """
                SELECT relative_path, path_key, sync_state, tombstone FROM files
                WHERE tombstone=0 AND sync_state <> 'DOWNLOAD_PENDING'
                    AND hydration_state='HYDRATED'
                """
            ).fetchall()
        missing = sorted(
            (row for row in known if row["path_key"] not in seen),
            key=lambda row: str(row["relative_path"]).count("/"),
            reverse=True,
        )
        for row in missing:
            await self.engine.local_deleted(str(row["relative_path"]))
            changed += 1
        return changed
