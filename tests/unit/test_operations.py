from __future__ import annotations

import random

from quarksync.db.database import Database
from quarksync.sync.operations import OperationQueue, OperationStatus, OperationType, retry_delay


def database(tmp_path):
    value = Database(tmp_path / "state.db")
    value.migrate()
    return value


def test_queue_persists_and_recovers_running_operations(tmp_path):
    db = database(tmp_path)
    queue = OperationQueue(db)
    operation_id = queue.enqueue(OperationType.UPLOAD, "work/report.docx")
    claimed = queue.claim_due()
    assert claimed and claimed.id == operation_id
    assert claimed.status is OperationStatus.RUNNING

    restarted = OperationQueue(db)
    assert restarted.recover_interrupted() == 1
    recovered = restarted.claim_due()
    assert recovered and recovered.id == operation_id


def test_writeback_debounce_resets_due_time(tmp_path):
    queue = OperationQueue(database(tmp_path))
    first = queue.enqueue(OperationType.UPLOAD, "a.txt", delay_seconds=10, debounce=True)
    second = queue.enqueue(
        OperationType.UPLOAD,
        "a.txt",
        payload={"cache_key": "new"},
        delay_seconds=60,
        debounce=True,
    )
    assert first == second
    pending = queue.pending()
    assert len(pending) == 1
    assert pending[0].payload == {"cache_key": "new"}


def test_retry_backoff_has_jitter_and_cap():
    value = retry_delay(0, rng=random.Random(1))  # noqa: S311 - deterministic jitter test
    assert 4 <= value <= 6
    assert retry_delay(  # noqa: S311 - deterministic jitter test
        99, maximum=100, rng=random.Random(1)  # noqa: S311 - deterministic jitter test
    ) <= 100
