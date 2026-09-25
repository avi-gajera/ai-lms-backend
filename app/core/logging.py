"""Structured logging with a per-request correlation id.

`request_id_var` is set by the HTTP middleware (and by Celery tasks with the task id), so every
log line emitted while handling a request carries the same id — including lines from services.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id_var.get():
            payload["request_id"] = rid
        # Anything passed via `extra={...}` becomes a structured field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        rid = request_id_var.get()
        prefix = f"[{rid[:8]}] " if rid else ""
        return f"{prefix}{base}" + (f" {extras}" if extras else "")


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_logs else TextFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party libraries are noisy at INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_worker_logging() -> None:
    """Configure logging from settings (API startup and Celery worker import)."""
    from app.config import get_settings

    s = get_settings()
    configure_logging(s.log_level, s.log_json)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
