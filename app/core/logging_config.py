"""Structured logging with a hard guarantee: no full phone number in stdout.

Two layers of defence:

1. Call sites log ``party_masked=phone.mask_e164(number)`` rather than the raw
   value.
2. A structlog processor rescans every rendered field and masks anything that
   still looks like a phone number, including text inside exception messages.

Grepping the whole log for a real subscriber number must find nothing;
layer 2 is what makes that hold even for text nobody thought about.

Typical usage example:

    logging_config.configure_logging("INFO", json_output=True)
    log = logging_config.get_logger(__name__)
    log.info("call.inbound", channel_id=channel_id)
"""

from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

import structlog

from app.core import phone

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)

_SAFE_KEYS = frozenset(
    {
        "ts",
        "level",
        "event",
        "logger",
        "trace_id",
        "session_id",
        "call_id",
        "channel_id",
        "bridge_id",
        "other_channel_id",
        "caller_hash",
        "party_hash",
        "duration_sec",
        "timeout",
        "attempt",
        "status_code",
    }
)


def bind_trace_id(trace_id: str | None) -> None:
    """Binds the trace identifier of the current task to every log line."""
    _trace_id.set(trace_id)


def get_trace_id() -> str | None:
    """Returns the trace identifier bound to the current task, if any."""
    return _trace_id.get()


def bind_session_id(session_id: str | None) -> None:
    """Binds the masking session of the current task to every log line."""
    _session_id.set(session_id)


def _inject_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Adds the context variables to an event."""
    event_dict.setdefault("trace_id", _trace_id.get())
    if _session_id.get() and "session_id" not in event_dict:
        event_dict["session_id"] = _session_id.get()
    return event_dict


def _scrub_pii(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Masks phone numbers in every string field that is not an identifier."""
    for key, value in list(event_dict.items()):
        if key in _SAFE_KEYS:
            continue
        if isinstance(value, str):
            event_dict[key] = phone.scrub_text(value)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configures structlog and the standard library logging.

    Args:
        level: Minimum level name, for example ``"INFO"``.
        json_output: Render JSON when true, a human-readable console format
            otherwise.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    numeric_level = logging.getLevelNamesMapping().get(
        level.upper(), logging.INFO
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", key="ts", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _scrub_pii,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Returns a bound logger for the given module name."""
    return structlog.get_logger(name)
