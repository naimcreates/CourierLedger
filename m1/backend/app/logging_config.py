"""Structured JSON logging for the M1 backend.

One JSON object per line on stdout: timestamp, level, logger, message,
plus any extra fields passed via the ``extra`` dict. No request bodies,
passwords, tokens or financial content are ever logged by this module;
callers must not pass secrets as extra fields.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
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
