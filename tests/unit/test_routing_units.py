from __future__ import annotations

import uuid

from app.ari import stasis
from app.core import config
from app.services import routing


def _candidate(code: str | None) -> routing.Candidate:
    return routing.Candidate(
        session_id=uuid.uuid4(),
        ext_code=code,
        callee_e164="+77019876543",
        direction="a2b",
        max_calls=None,
    )


def _app(endpoint_template: str) -> stasis.MaskingStasisApp:
    settings = config.Settings(
        _env_file=None, endpoint_template=endpoint_template
    )
    return stasis.MaskingStasisApp(settings)


def test_select_by_code_picks_the_matching_session() -> None:
    first, second = _candidate("1111"), _candidate("2222")
    assert routing.select_by_code([first, second], "2222") is second


def test_select_by_code_rejects_unknown_code() -> None:
    assert routing.select_by_code([_candidate("1111")], "9999") is None


def test_select_by_code_ignores_sessions_without_a_code() -> None:
    assert routing.select_by_code([_candidate(None)], "") is None


def test_endpoint_template_prototype() -> None:
    app = _app("PJSIP/{digits}")
    assert app._endpoint_for("+77019876543") == "PJSIP/77019876543"


def test_endpoint_template_production_trunk() -> None:
    app = _app("PJSIP/{number}@trunk-operator")
    endpoint = app._endpoint_for("+77019876543")
    assert endpoint == "PJSIP/+77019876543@trunk-operator"


def test_q850_causes_map_to_journal_statuses() -> None:
    assert stasis.CAUSE_TO_STATUS[17] == "busy"
    assert stasis.CAUSE_TO_STATUS[19] == "no_answer"
    assert stasis.CAUSE_TO_STATUS[21] == "rejected"
    assert stasis.CAUSE_TO_STATUS[16] == "answered"
