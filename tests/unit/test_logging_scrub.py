"""No full phone number ever reaches stdout, whatever field it hides in."""

from __future__ import annotations

import json

import pytest
import structlog

from app.core import logging_config


@pytest.fixture
def captured() -> list[str]:
    """Redirects structlog output into a list of rendered lines."""
    lines: list[str] = []

    class _Logger:
        def msg(self, message: str) -> None:
            lines.append(message)

        log = err = debug = info = warning = error = critical = msg

    class _Factory:
        def __call__(self, *args: object, **kwargs: object) -> _Logger:
            return _Logger()

    logging_config.configure_logging("DEBUG", json_output=True)
    structlog.configure(
        logger_factory=_Factory(), cache_logger_on_first_use=False
    )
    return lines


def test_number_in_any_field_is_masked(captured: list[str]) -> None:
    logging_config.bind_trace_id("trace-1")
    logging_config.get_logger("test").info(
        "call.inbound", caller="+77011234567", note="from 77019876543"
    )
    payload = json.loads(captured[-1])
    assert "77011234567" not in captured[-1]
    assert "77019876543" not in captured[-1]
    assert payload["trace_id"] == "trace-1"
    assert payload["event"] == "call.inbound"


def test_exception_text_is_scrubbed(captured: list[str]) -> None:
    logging_config.get_logger("test").error(
        "failure", error="cannot route +77011234567"
    )
    assert "77011234567" not in captured[-1]


def test_identifiers_are_not_mangled() -> None:
    event = {
        "session_id": "b3f1c2d4-1111-2222-3333-444455556666",
        "caller_hash": "a" * 64,
        "channel_id": "1712345678.123",
        "duration_sec": 42,
    }
    result = logging_config._scrub_pii(None, "info", dict(event))
    assert result == event
