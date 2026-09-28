from __future__ import annotations

import logging

from quarksync.logging_config import RedactingFilter


def test_redacting_filter_preserves_numeric_format_arguments() -> None:
    record = logging.LogRecord(
        "quarksync.test",
        logging.INFO,
        __file__,
        1,
        "HTTP status %d; %s",
        (207, "Cookie=session-secret"),
        None,
    )

    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "HTTP status 207; Cookie=<redacted>"
    assert record.args[0] == 207


def test_redacting_filter_handles_mapping_arguments() -> None:
    record = logging.LogRecord(
        "quarksync.test",
        logging.INFO,
        __file__,
        1,
        "HTTP %(status)d; %(detail)s",
        {"status": 200, "detail": "token=secret"},
        None,
    )

    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "HTTP 200; token=<redacted>"
