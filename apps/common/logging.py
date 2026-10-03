"""
Structured logging with a propagated context envelope (ADR-A37, SRS NFR-10).

Six identifiers must survive the whole path so an incident can be reconstructed
without correlating timestamps by hand:

    HTTP → service → transaction → outbox → Celery → consumer → AI provider

    request_id   one HTTP request
    actor_id     the authenticated user (bound by auth, P1)
    company_id   the active tenant (bound by tenancy middleware, P1)
    task_id      the Celery task run
    event_id     the outbox event being dispatched
    ai_run_id    the AIRun being executed (P11)

The context lives in one ContextVar. A ContextVar cannot cross a process
boundary, so Celery propagation is explicit: the publisher copies the envelope
into task headers and the worker restores it (see config/celery.py). Nothing
relies on request-local state surviving into a pooled worker.

The reset in `task_postrun` is not optional. Without it a value set by one task
persists into the next task on the same worker, and one user's work is logged
under another user's identifiers.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, replace
from typing import Any

HEADER_KEY = "careerai_log_context"


@dataclass(frozen=True)
class LogContext:
    """Correlation identifiers for one unit of work."""

    request_id: str | None = None
    actor_id: int | None = None
    company_id: int | None = None
    task_id: str | None = None
    event_id: int | None = None
    ai_run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Only the fields that are set. An envelope of nulls is noise."""
        return {key: value for key, value in asdict(self).items() if value is not None}


_EMPTY = LogContext()

_context: contextvars.ContextVar[LogContext | None] = contextvars.ContextVar(
    "log_context", default=None
)


def get_context() -> LogContext:
    return _context.get() or _EMPTY


def bind(**fields: Any) -> contextvars.Token:
    """Add fields to the current context. Returns a token for `reset`."""
    return _context.set(replace(get_context(), **fields))


def reset(token: contextvars.Token) -> None:
    _context.reset(token)


def set_context(**fields: Any) -> contextvars.Token:
    """Replace the context entirely with these fields. Used at task entry."""
    return _context.set(LogContext(**fields))


def clear() -> contextvars.Token:
    """Discard the context. Used at task exit, where it is not optional."""
    return _context.set(None)


# Attributes LogRecord always carries; everything else on the record was put
# there by the caller and is worth emitting.
_STANDARD_ATTRS = frozenset(
    [
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    ]
)


class RequestIDFilter(logging.Filter):
    """Attaches the correlation envelope to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.log_context = get_context().as_dict()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line. Machine-parseable, greppable, no ceremony."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "log_context", None) or {})

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != "log_context" and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)
