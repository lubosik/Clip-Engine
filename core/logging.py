"""
core/logging.py — structured JSON-lines logging setup.

Call configure_logging() once at process startup (producer/run.py,
scheduler entrypoints, web startup).  After that, use stdlib logging
everywhere — the handler emits JSON lines to stdout.

Each log record includes: timestamp (ISO 8601 UTC), level, logger name,
message, and any extra kwargs passed via the `extra` argument.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Include any extra fields passed via extra={...}
        skip = logging.LogRecord.__dict__.keys() | {
            "args", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "message", "module", "msecs",
            "msg", "name", "pathname", "process", "processName", "relativeCreated",
            "stack_info", "taskName", "thread", "threadName",
        }
        for key, val in record.__dict__.items():
            if key not in skip:
                payload[key] = val

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """
    Configure root logger to emit JSON lines to stdout.
    Call once at process startup.  Idempotent (won't double-add handlers).
    """
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, _JsonFormatter)
           for h in root.handlers):
        return  # already configured

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "apify_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — same as logging.getLogger(name)."""
    return logging.getLogger(name)
