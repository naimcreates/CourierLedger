"""Structured JSON logging for the M1 backend.

One JSON object per line on stdout: timestamp, level, logger, message,
plus any extra fields passed via the ``extra`` dict. This module formats
whatever it is given — it has no allow-list on extra fields — so it
must never be handed secrets: callers must not pass request bodies,
passwords, tokens or financial content as extra fields. (Phase 1,
B-F2: the password-reset link used to be logged by a caller; it no
longer is, and no log line may contain a raw token or reset link.)

Phase 2 (Part 8): the request-id middleware sets a per-request id
in a contextvar; the formatter adds it as ``request_id`` to every
log line for that request. The id itself is an opaque UUID (or a
client-supplied token that passed the middleware's character
allow-list), never user data.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Phase 2 (Part 8): the request id for the request currently being
# handled, if any. The request-id middleware (app/main.py) sets it
# per request; JsonFormatter copies it into every log line emitted
# while the request is in flight, so a single id ties together all
# log lines for one request. Outside a request (startup, background
# work) it is None and the field is omitted.
request_id_var: ContextVar[str | None] = ContextVar("courierledger_request_id", default=None)


def set_request_id(request_id: str | None) -> None:
    request_id_var.set(request_id)


def get_request_id() -> str | None:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        # Merge structured extras, skipping stdlib record internals.
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Quieten uvicorn's default plain-text access logs into the same stream;
    # they keep their own format, which is acceptable for M0.
    logging.getLogger("uvicorn.error").handlers = [handler]
    logging.getLogger("uvicorn.error").propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
