from __future__ import annotations

import pytest

from quarksync.db.database import Database, utc_now
from quarksync.sync.states import InvalidStateTransition, SyncState, SyncStateMachine


def test_transitions_are_centralized(tmp_path):
    database = Database(tmp_path / "state.db")
    database.migrate()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO files(relative_path, path_key, remote_path, sync_state, updated_at)
            VALUES ('a.txt', 'a.txt', 'a.txt', 'REMOTE_ONLY', ?)
            """,
            (utc_now(),),
        )
    machine = SyncStateMachine(database)
    machine.transition("a.txt", SyncState.HYDRATING)
    machine.transition("a.txt", SyncState.SYNCED)
    machine.transition("a.txt", SyncState.LOCAL_DIRTY)
    with pytest.raises(InvalidStateTransition):
        machine.transition("a.txt", SyncState.REMOTE_ONLY)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT sync_state, dirty FROM files WHERE path_key = 'a.txt'"
        ).fetchone()
    assert tuple(row) == ("LOCAL_DIRTY", 1)

