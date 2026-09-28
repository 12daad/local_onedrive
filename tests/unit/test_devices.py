from __future__ import annotations

from quarksync.backend.base import RemoteEntry
from quarksync.db.database import Database
from quarksync.status.devices import DeviceStatusStore


class _ExistingLayoutBackend:
    def __init__(self) -> None:
        self.directories: list[str] = []

    async def mkdir(self, path: str) -> None:
        self.directories.append(path)

    async def stat(self, path: str) -> RemoteEntry:
        return RemoteEntry(
            path=path,
            href=f"/dav/{path}",
            display_name="schema.json",
            is_dir=False,
            size=21,
            etag='"schema-v1"',
        )

    async def upload(self, *_args, **_kwargs) -> RemoteEntry:
        raise AssertionError("an existing schema must not be rewritten at startup")


async def test_existing_remote_schema_is_not_rewritten(tmp_path) -> None:
    database = Database(tmp_path / "state.db")
    database.migrate()
    backend = _ExistingLayoutBackend()
    devices = DeviceStatusStore(
        tmp_path / "data", database, backend, device_name="test-device"
    )

    await devices.ensure_remote_layout()

    assert backend.directories == [".quarksync-meta", ".quarksync-meta/devices"]
