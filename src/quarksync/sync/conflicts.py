from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from quarksync.db.database import Database, utc_now


def conflict_name(path: Path, device_name: str, when: datetime | None = None) -> Path:
    moment = (when or datetime.now()).strftime("%Y-%m-%d %H%M%S")
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem} (conflict {device_name} {moment}){suffix}")


class ConflictManager:
    def __init__(self, database: Database, device_name: str) -> None:
        self.database = database
        self.device_name = device_name

    @staticmethod
    def is_conflict(*, base_etag: str | None, remote_etag: str | None, local_changed: bool) -> bool:
        return local_changed and base_etag is not None and remote_etag != base_etag

    def keep_local_copy(
        self, local_path: Path, relative_path: str, remote_etag: str | None, base_etag: str | None
    ) -> Path:
        destination = conflict_name(local_path, self.device_name)
        counter = 2
        while destination.exists():
            destination = destination.with_name(f"{destination.stem} {counter}{destination.suffix}")
            counter += 1
        shutil.copy2(local_path, destination)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO conflicts(
                    path, local_path, remote_etag, base_etag, conflict_copy_path,
                    created_at, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relative_path,
                    str(local_path),
                    remote_etag,
                    base_etag,
                    str(destination),
                    utc_now(),
                    "KEEP_BOTH: preserved local content before applying remote version",
                ),
            )
        return destination

