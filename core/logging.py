"""JSON logging with a trace id on every line.

Every component logs one JSON object per line so a whole flow can be pulled out
of CloudWatch by `trace_id` (docs/03 §1). Message content never goes to
CloudWatch — model requests and responses are session records in S3 instead
(docs/06 §5.2 step 7), where the owner controls retention and deletion.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

# Field names that must never reach CloudWatch, whatever a caller passes.
_FORBIDDEN_FIELDS = frozenset(
    {
        "text",
        "content",
        "message",
        "messages",
        "system",
        "reply",
        "code",
        "stdin",
        "stdout",
        "stderr",
        "diff",
        "reasoning_content",
        "api_key",
        "token",
    }
)


def set_trace_id(trace_id: str | None) -> None:
    """Bind a trace id for the current execution context."""
    _trace_id.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id.get()


class StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Resolve `sys.stdout` at emit time rather than at construction.

    A handler that captures the stream when it is built keeps writing to a
    stale file object if anything replaces stdout later — which is what pytest's
    capture does, and what some Lambda runtimes do on cold start.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, value: Any) -> None:  # the base class assigns in __init__
        pass


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = _trace_id.get()
        if trace_id:
            payload["trace_id"] = trace_id
        stage = os.environ.get("STAGE")
        if stage:
            payload["stage"] = stage

        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(_scrub(extra))

        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "error")
            # The message, not the traceback: tracebacks can carry argument values.
            payload["exc"] = str(record.exc_info[1])

        return json.dumps(payload, default=str, separators=(",", ":"))


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop anything that could carry owner or model content into logs."""
    return {k: ("<redacted>" if k in _FORBIDDEN_FIELDS else v) for k, v in fields.items()}


def get_logger(name: str) -> logging.Logger:
    """A logger that writes JSON lines to stdout exactly once.

    Lambda installs its own root handler, so we attach ours to the named logger
    and stop propagation rather than reconfiguring the root.
    """
    logger = logging.getLogger(name)
    if not getattr(logger, "_agent_configured", False):
        handler = StdoutHandler()
        handler.setFormatter(JsonFormatter())
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger._agent_configured = True  # type: ignore[attr-defined]
    return logger


def log(logger: logging.Logger, level: int, msg: str, /, **fields: Any) -> None:
    """Log `msg` with structured `fields` merged into the JSON object."""
    logger.log(level, msg, extra={"fields": fields})


def info(logger: logging.Logger, msg: str, /, **fields: Any) -> None:
    log(logger, logging.INFO, msg, **fields)


def warning(logger: logging.Logger, msg: str, /, **fields: Any) -> None:
    log(logger, logging.WARNING, msg, **fields)


def error(logger: logging.Logger, msg: str, /, **fields: Any) -> None:
    log(logger, logging.ERROR, msg, **fields)
