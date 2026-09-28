from datetime import datetime
from pathlib import Path

from quarksync.sync.conflicts import ConflictManager, conflict_name


def test_conflict_detection_requires_both_sides_changed():
    assert ConflictManager.is_conflict(
        base_etag='"old"', remote_etag='"new"', local_changed=True
    )
    assert not ConflictManager.is_conflict(
        base_etag='"old"', remote_etag='"old"', local_changed=True
    )


def test_conflict_filename_preserves_compound_extension():
    result = conflict_name(
        Path("archive.tar.gz"), "DESKTOP-A", datetime(2026, 9, 27, 13, 45, 0)
    )
    assert result.name == "archive (conflict DESKTOP-A 2026-09-27 134500).tar.gz"

