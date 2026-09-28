from __future__ import annotations

from enum import StrEnum

from quarksync.db.database import Database, utc_now


class SyncState(StrEnum):
    REMOTE_ONLY = "REMOTE_ONLY"
    HYDRATING = "HYDRATING"
    SYNCED = "SYNCED"
    LOCAL_DIRTY = "LOCAL_DIRTY"
    UPLOAD_PENDING = "UPLOAD_PENDING"
    UPLOADING = "UPLOADING"
    DOWNLOAD_PENDING = "DOWNLOAD_PENDING"
    CONFLICT = "CONFLICT"
    DELETE_PENDING = "DELETE_PENDING"
    ERROR = "ERROR"


_TRANSITIONS: dict[SyncState, frozenset[SyncState]] = {
    SyncState.REMOTE_ONLY: frozenset(
        {SyncState.HYDRATING, SyncState.DELETE_PENDING, SyncState.ERROR}
    ),
    SyncState.HYDRATING: frozenset(
        {SyncState.SYNCED, SyncState.REMOTE_ONLY, SyncState.ERROR}
    ),
    SyncState.SYNCED: frozenset(
        {
            SyncState.LOCAL_DIRTY,
            SyncState.DOWNLOAD_PENDING,
            SyncState.DELETE_PENDING,
            SyncState.CONFLICT,
            SyncState.ERROR,
        }
    ),
    SyncState.LOCAL_DIRTY: frozenset(
        {SyncState.UPLOAD_PENDING, SyncState.CONFLICT, SyncState.DELETE_PENDING, SyncState.ERROR}
    ),
    SyncState.UPLOAD_PENDING: frozenset(
        {SyncState.UPLOADING, SyncState.LOCAL_DIRTY, SyncState.CONFLICT, SyncState.ERROR}
    ),
    SyncState.UPLOADING: frozenset(
        {SyncState.SYNCED, SyncState.UPLOAD_PENDING, SyncState.CONFLICT, SyncState.ERROR}
    ),
    SyncState.DOWNLOAD_PENDING: frozenset(
        {SyncState.SYNCED, SyncState.CONFLICT, SyncState.ERROR}
    ),
    SyncState.CONFLICT: frozenset(
        {SyncState.LOCAL_DIRTY, SyncState.SYNCED, SyncState.DELETE_PENDING, SyncState.ERROR}
    ),
    SyncState.DELETE_PENDING: frozenset(
        {SyncState.LOCAL_DIRTY, SyncState.SYNCED, SyncState.ERROR}
    ),
    SyncState.ERROR: frozenset(
        {
            SyncState.REMOTE_ONLY,
            SyncState.LOCAL_DIRTY,
            SyncState.UPLOAD_PENDING,
            SyncState.DOWNLOAD_PENDING,
            SyncState.DELETE_PENDING,
            SyncState.CONFLICT,
            SyncState.SYNCED,
        }
    ),
}


class InvalidStateTransition(RuntimeError):
    pass


class SyncStateMachine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def transition(
        self,
        relative_path: str,
        target: SyncState,
        *,
        error: str | None = None,
        force: bool = False,
    ) -> None:
        path_key = relative_path.casefold()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT sync_state FROM files WHERE path_key = ?", (path_key,)
            ).fetchone()
            if row is None:
                raise KeyError(relative_path)
            current = SyncState(row[0])
            if not force and target != current and target not in _TRANSITIONS[current]:
                raise InvalidStateTransition(f"{current} -> {target} for {relative_path}")
            dirty = target in {
                SyncState.LOCAL_DIRTY,
                SyncState.UPLOAD_PENDING,
                SyncState.UPLOADING,
                SyncState.CONFLICT,
                SyncState.DELETE_PENDING,
            }
            tombstone = target == SyncState.DELETE_PENDING
            connection.execute(
                """
                UPDATE files
                SET sync_state = ?, dirty = ?, tombstone = ?, last_error = ?, updated_at = ?
                WHERE path_key = ?
                """,
                (target.value, int(dirty), int(tombstone), error, utc_now(), path_key),
            )

    @staticmethod
    def can_transition(current: SyncState, target: SyncState) -> bool:
        return target == current or target in _TRANSITIONS[current]

