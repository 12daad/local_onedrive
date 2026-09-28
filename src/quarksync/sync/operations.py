from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from quarksync.db.database import Database, utc_now


class OperationType(StrEnum):
    UPLOAD = "UPLOAD"
    DOWNLOAD = "DOWNLOAD"
    REMOTE_DELETE = "REMOTE_DELETE"
    DELETE = "DELETE"
    MOVE = "MOVE"
    MKDIR = "MKDIR"


class OperationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationSuperseded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Operation:
    id: int
    type: OperationType
    path: str
    destination_path: str | None
    payload: dict[str, object]
    status: OperationStatus
    retry_count: int
    next_retry_at: str
    last_error: str | None


def retry_delay(
    retry_count: int,
    *,
    maximum: float = 1800,
    rng: random.Random | None = None,
) -> float:
    schedule = (5, 15, 30, 60, 300)
    base = schedule[min(retry_count, len(schedule) - 1)]
    if retry_count >= len(schedule):
        base = min(maximum, schedule[-1] * 2 ** (retry_count - len(schedule) + 1))
    jitter = (rng or random).uniform(0.8, 1.2)
    return min(maximum, base * jitter)


class OperationQueue:
    def __init__(self, database: Database, *, max_backoff_seconds: int = 1800) -> None:
        self.database = database
        self.max_backoff_seconds = max_backoff_seconds

    def enqueue(
        self,
        operation_type: OperationType,
        path: str,
        *,
        destination_path: str | None = None,
        payload: dict[str, object] | None = None,
        delay_seconds: float = 0,
        debounce: bool = False,
    ) -> int:
        now = datetime.now(UTC)
        due = (now + timedelta(seconds=delay_seconds)).isoformat()
        serialized = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            if debounce:
                existing = connection.execute(
                    """
                    SELECT id FROM operations
                    WHERE type = ? AND path = ? AND status = 'PENDING'
                    """,
                    (operation_type.value, path),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE operations SET destination_path = ?, payload_json = ?,
                            next_retry_at = ?, updated_at = ?, retry_count = 0, last_error = NULL
                        WHERE id = ?
                        """,
                        (destination_path, serialized, due, now.isoformat(), existing[0]),
                    )
                    return int(existing[0])
            cursor = connection.execute(
                """
                INSERT INTO operations(
                    type, path, destination_path, payload_json, status,
                    next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    operation_type.value,
                    path,
                    destination_path,
                    serialized,
                    due,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE operations SET status = 'PENDING', next_retry_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, 'Recovered after interrupted process')
                WHERE status = 'RUNNING'
                """,
                (now, now),
            )
            return cursor.rowcount

    def claim_due(self, operation_type: OperationType | None = None) -> Operation | None:
        now = utc_now()
        with self.database.transaction() as connection:
            query = """
                SELECT * FROM operations
                WHERE status = 'PENDING' AND next_retry_at <= ?
            """
            parameters: list[object] = [now]
            if operation_type:
                query += " AND type = ?"
                parameters.append(operation_type.value)
            query += " ORDER BY next_retry_at, id LIMIT 1"
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE operations SET status = 'RUNNING', updated_at = ? "
                "WHERE id = ? AND status = 'PENDING'",
                (now, row["id"]),
            ).rowcount
            if changed != 1:
                return None
            return self._from_row(row, status=OperationStatus.RUNNING)

    def complete(self, operation_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE operations SET status = 'COMPLETE', updated_at = ?, last_error = NULL "
                "WHERE id = ? AND status = 'RUNNING'",
                (utc_now(), operation_id),
            )

    def retry(self, operation_id: int, error: str) -> float:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT retry_count FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            delay = retry_delay(row[0], maximum=self.max_backoff_seconds)
            due = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """
                UPDATE operations SET status = 'PENDING', retry_count = retry_count + 1,
                    next_retry_at = ?, updated_at = ?, last_error = ? WHERE id = ?
                """,
                (due, utc_now(), error[:2000], operation_id),
            )
            return delay

    def fail(self, operation_id: int, error: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE operations SET status = 'FAILED', updated_at = ?, last_error = ? "
                "WHERE id = ?",
                (utc_now(), error[:2000], operation_id),
            )

    def cancel(self, operation_id: int, reason: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE operations SET status='CANCELLED', updated_at=?, last_error=? "
                "WHERE id=? AND status IN ('PENDING', 'RUNNING')",
                (utc_now(), reason[:2000], operation_id),
            )

    def cancel_pending(self, operation_type: OperationType, path: str, reason: str) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE operations SET status='CANCELLED', updated_at=?, last_error=?
                WHERE type=? AND path=? AND status='PENDING'
                """,
                (utc_now(), reason[:2000], operation_type.value, path),
            )
            return cursor.rowcount

    def pending(self) -> list[Operation]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operations WHERE status IN ('PENDING', 'RUNNING') "
                "ORDER BY next_retry_at, id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row, *, status: OperationStatus | None = None) -> Operation:
        return Operation(
            id=row["id"],
            type=OperationType(row["type"]),
            path=row["path"],
            destination_path=row["destination_path"],
            payload=json.loads(row["payload_json"]),
            status=status or OperationStatus(row["status"]),
            retry_count=row["retry_count"],
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
        )
