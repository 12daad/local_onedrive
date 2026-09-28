from __future__ import annotations

from quarksync.backend.webdav import OpenListWebDAVBackend
from quarksync.db.database import Database
from quarksync.sync.scanner import RemoteScanner


async def test_case_colliding_remote_names_are_not_silently_overwritten(
    tmp_path, mock_webdav_server
):
    state, url = mock_webdav_server
    state.files["quark/Report.txt"] = b"one"
    state.files["quark/report.TXT"] = b"two"
    database = Database(tmp_path / "state.db")
    database.migrate()
    backend = OpenListWebDAVBackend(url, "syncuser", "secret", remote_root="quark")
    scanner = RemoteScanner(database, backend)
    try:
        await scanner.eager_enumerate()
    finally:
        await backend.close()
    with database.connect() as connection:
        snapshots = connection.execute(
            "SELECT COUNT(*) FROM remote_snapshot WHERE path_key='report.txt'"
        ).fetchone()[0]
        conflicts = connection.execute(
            "SELECT COUNT(*) FROM conflicts WHERE status='OPEN'"
        ).fetchone()[0]
    assert snapshots == 1
    assert conflicts == 1

