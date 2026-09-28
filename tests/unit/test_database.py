from quarksync.db.database import Database


def test_migrations_are_idempotent(tmp_path):
    database = Database(tmp_path / "state.db")
    database.migrate()
    database.migrate()
    assert database.integrity_check() == "ok"
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"files", "operations", "remote_snapshot", "devices", "conflicts"} <= tables

