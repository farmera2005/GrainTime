"""Structured JSON logging with a redaction safety net.

Code must never log passwords or connection strings in the first place; the
redaction filter is a second line of defence in case a driver error message
or exception text echoes one back.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_REDACT_PATTERNS = [
    # ODBC-style key=value pairs, braced or not.
    (re.compile(r"(?i)\b(PWD|PASSWORD)\s*=\s*\{(?:[^}]|\}\})*\}"), r"\1=***"),
    (re.compile(r"(?i)\b(PWD|PASSWORD)\s*=\s*[^;\s\"']*"), r"\1=***"),
    # URL credentials: scheme://user:pass@host
    (re.compile(r"(://[^:/@\s]+):[^@\s]+@"), r"\1:***@"),
    # JSON-ish "password": "..."
    (re.compile(r'(?i)("(?:password|pwd|secret|token)[a-z_]*"\s*:\s*)"[^"]*"'), r'\1"***"'),
]

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                entry[k] = v
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, default=str))


def setup_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"graintime.{name}")
