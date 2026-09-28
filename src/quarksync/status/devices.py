from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from quarksync import __version__
from quarksync.backend.webdav import RemoteNotFoundError
from quarksync.db.database import Database, utc_now

if TYPE_CHECKING:
    from quarksync.backend.base import StorageBackend

META_ROOT = ".quarksync-meta"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    schema_version: int
    device_id: str
    device_name: str
    hostname: str
    client_version: str


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    schema_version: int
    device_id: str
    device_name: str
    hostname: str
    client_version: str
    last_heartbeat: str
    last_successful_sync: str | None
    pending_uploads: int
    pending_downloads: int
    pending_deletes: int
    conflicts: int
    last_error: str | None


class DeviceStatusStore:
    def __init__(
        self,
        data_dir: Path,
        database: Database,
        backend: StorageBackend,
        *,
        device_name: str,
    ) -> None:
        self.data_dir = data_dir
        self.database = database
        self.backend = backend
        self.identity = self._load_or_create_identity(device_name)

    def _load_or_create_identity(self, device_name: str) -> DeviceIdentity:
        path = self.data_dir / "device.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return DeviceIdentity(**raw)
        identity = DeviceIdentity(
            schema_version=1,
            device_id=str(uuid.uuid4()),
            device_name=device_name,
            hostname=socket.gethostname(),
            client_version=__version__,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(identity), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
        return identity

    def current_status(self, last_error: str | None = None) -> DeviceStatus:
        with self.database.connect() as connection:
            counts = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT type, COUNT(*) FROM operations "
                    "WHERE status IN ('PENDING', 'RUNNING') GROUP BY type"
                )
            }
            conflicts = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN'"
            ).fetchone()[0]
            last_sync = connection.execute(
                "SELECT MAX(last_sync_at) FROM files WHERE last_sync_at IS NOT NULL"
            ).fetchone()[0]
        return DeviceStatus(
            **asdict(self.identity),
            last_heartbeat=utc_now(),
            last_successful_sync=last_sync,
            pending_uploads=counts.get("UPLOAD", 0),
            pending_downloads=counts.get("DOWNLOAD", 0) + counts.get("REMOTE_DELETE", 0),
            pending_deletes=counts.get("DELETE", 0),
            conflicts=conflicts,
            last_error=last_error,
        )

    async def ensure_remote_layout(self) -> None:
        await self.backend.mkdir(META_ROOT)
        await self.backend.mkdir(f"{META_ROOT}/devices")
        try:
            await self.backend.stat(f"{META_ROOT}/schema.json")
            return
        except RemoteNotFoundError:
            pass
        schema_path = self.data_dir / "schema.json"
        schema_path.write_text('{"schema_version":1}\n', encoding="utf-8")
        await self._upload_control(schema_path, f"{META_ROOT}/schema.json")

    async def publish(self, last_error: str | None = None) -> DeviceStatus:
        status = self.current_status(last_error)
        local = self.data_dir / f"heartbeat-{self.identity.device_id}.json"
        local.write_text(
            json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        await self._upload_control(
            local, f"{META_ROOT}/devices/{self.identity.device_id}.json"
        )
        self._save(status)
        return status

    async def refresh_all(self) -> list[DeviceStatus]:
        statuses: list[DeviceStatus] = []
        directory = await self.backend.list_dir(f"{META_ROOT}/devices")
        temp_dir = self.data_dir / "device-status"
        temp_dir.mkdir(parents=True, exist_ok=True)
        for entry in directory:
            if entry.is_dir or not entry.path.endswith(".json"):
                continue
            destination = temp_dir / PureDeviceName(entry.path)
            await self.backend.download(entry.path, destination, expected_etag=entry.etag)
            raw = json.loads(destination.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1:
                continue
            status = DeviceStatus(**raw)
            self._save(status)
            statuses.append(status)
        return statuses

    async def _upload_control(self, local: Path, remote: str) -> None:
        try:
            current = await self.backend.stat(remote)
            expected_etag = current.etag
        except RemoteNotFoundError:
            expected_etag = None
        await self.backend.upload(local, remote, expected_etag=expected_etag)

    def _save(self, status: DeviceStatus) -> None:
        raw = json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO devices(
                    device_id, device_name, hostname, client_version, last_heartbeat,
                    last_successful_sync, pending_uploads, pending_downloads,
                    pending_deletes, conflicts, last_error, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name=excluded.device_name, hostname=excluded.hostname,
                    client_version=excluded.client_version,
                    last_heartbeat=excluded.last_heartbeat,
                    last_successful_sync=excluded.last_successful_sync,
                    pending_uploads=excluded.pending_uploads,
                    pending_downloads=excluded.pending_downloads,
                    pending_deletes=excluded.pending_deletes,
                    conflicts=excluded.conflicts, last_error=excluded.last_error,
                    raw_json=excluded.raw_json
                """,
                (
                    status.device_id,
                    status.device_name,
                    status.hostname,
                    status.client_version,
                    status.last_heartbeat,
                    status.last_successful_sync,
                    status.pending_uploads,
                    status.pending_downloads,
                    status.pending_deletes,
                    status.conflicts,
                    status.last_error,
                    raw,
                ),
            )


def PureDeviceName(path: str) -> str:
    # Device IDs are UUIDs, but basename extraction also contains any unexpected server value.
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def classify_presence(last_heartbeat: str, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    age = (current - datetime.fromisoformat(last_heartbeat)).total_seconds()
    if age <= 120:
        return "online"
    if age <= 600:
        return "stale"
    return "offline"
