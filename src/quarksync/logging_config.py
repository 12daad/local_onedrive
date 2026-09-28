from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_data_path

_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|cookie|password|token)(\s*[:=]\s*)([^\s,;]+)"
)


def redact(value: object) -> str:
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", str(value))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def bootstrap_log_dir() -> Path:
    """Resolve a log path before full configuration validation is available."""
    configured = os.getenv("QUARKSYNC_LOG_DIR")
    if configured:
        return Path(configured)
    return Path(user_data_path("QuarkSync", appauthor=False)) / "logs"


def configure_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(process)d:%(threadName)s] %(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers.append(
        RotatingFileHandler(
            log_dir / "quarksync.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
    redactor = RedactingFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO), handlers=handlers, force=True
    )
