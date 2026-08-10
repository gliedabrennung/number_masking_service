"""REST contract: session CRUD, masking of personal data, error codes."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

A = "+77011230001"
B = "+77011230002"
C = "+77011230003"


async def _add_number(client, e164: str = "+77172000101") -> None:
    response = await client.post(
        "/api/v1/numbers", json={"e164": e164, "provider": "test"}
    )
    assert response.status_code == 201, response.text


async def test_create_session_returns_a_proxy_number(api_client) -> None:
    """The happy path of session creation."""
    await _add_number(api_client)

    response = await api_client.post(
        "/api/v1/sessions",
        json={
            "party_a": A,
            "party_b": B,
            "ttl_seconds": 3600,
            "external_id": "order-4821",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["proxy_number"] == "+77172000101"
    assert body["status"] == "active"
    assert body["extension_code"] is None
    assert body["external_id"] == "order-4821"
    assert body["expires_at"] > body["created_at"]


async def test_response_never_contains_a_full_subscriber_number(
    api_client,
) -> None:
    await _add_number(api_client)
    response = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B}
    )

    raw = response.text
    assert "77011230001" not in raw
    assert "77011230002" not in raw
    masked = {party["number_masked"] for party in response.json()["parties"]}
    assert masked == {"+7701***0001", "+7701***0002"}


async def test_get_session_masks_numbers_too(api_client) -> None:
    await _add_number(api_client)
    created = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B}
    )
    session_id = created.json()["session_id"]

    response = await api_client.get(f"/api/v1/sessions/{session_id}")

    assert response.status_code == 200
    assert "77011230001" not in response.text
    assert response.json()["session_id"] == session_id


async def test_extend_ttl(api_client) -> None:
    await _add_number(api_client)
    created = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B, "ttl_seconds": 60}
    )
    session_id = created.json()["session_id"]

    response = await api_client.patch(
        f"/api/v1/sessions/{session_id}", json={"ttl_seconds": 7200}
    )

    assert response.status_code == 200
    assert response.json()["expires_at"] > created.json()["expires_at"]


async def test_close_session(api_client) -> None:
    """Closing a session early, seen from the API."""
    await _add_number(api_client)
    created = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B}
    )
    session_id = created.json()["session_id"]

    response = await api_client.delete(f"/api/v1/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert response.json()["closed_at"] is not None


async def test_extending_a_closed_session_conflicts(api_client) -> None:
    await _add_number(api_client)
    created = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B}
    )
    session_id = created.json()["session_id"]
    await api_client.delete(f"/api/v1/sessions/{session_id}")

    response = await api_client.patch(
        f"/api/v1/sessions/{session_id}", json={"ttl_seconds": 600}
    )

    assert response.status_code == 409
    assert response.json()["error"] == "session_not_active"


async def test_pool_exhaustion_is_409_with_a_stable_error_code(
    api_client,
) -> None:
    """An exhausted pool is a conflict, not a silent reuse."""
    await _add_number(api_client)
    await api_client.post("/api/v1/sessions", json={"party_a": A, "party_b": B})

    response = await api_client.post(
        "/api/v1/sessions",
        json={"party_a": A, "party_b": C, "allow_extension_code": False},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "no_number_available"


async def test_shared_number_returns_an_extension_code(api_client) -> None:
    """A shared number comes back with a PIN, seen from the API."""
    await _add_number(api_client)
    await api_client.post("/api/v1/sessions", json={"party_a": A, "party_b": B})

    response = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": C}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extension_code"] is not None
    assert len(body["extension_code"]) == 4
    assert body["promoted_session_ids"]


@pytest.mark.parametrize(
    "payload",
    [
        {"party_a": "not-a-number", "party_b": B},
        {"party_a": A, "party_b": A},
        {"party_a": A},
        {"party_a": A, "party_b": B, "ttl_seconds": 0},
        {"party_a": A, "party_b": B, "unexpected": 1},
    ],
)
async def test_invalid_payloads_are_422(api_client, payload: dict) -> None:
    await _add_number(api_client)
    response = await api_client.post("/api/v1/sessions", json=payload)
    assert response.status_code == 422
    assert "77011230001" not in response.text


async def test_missing_api_key_is_401(api_client) -> None:
    response = await api_client.post(
        "/api/v1/sessions",
        json={"party_a": A, "party_b": B},
        headers={"X-API-Key": ""},
    )
    assert response.status_code == 401


async def test_wrong_api_key_is_401(api_client) -> None:
    response = await api_client.get(
        "/api/v1/numbers", headers={"X-API-Key": "nope"}
    )
    assert response.status_code == 401


async def test_unknown_session_is_404(api_client) -> None:
    response = await api_client.get(
        "/api/v1/sessions/8ba9a2fe-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "session_not_found"


async def test_pool_view_reports_cooldown(api_client) -> None:
    """The pool view reports a released number as being in cooldown."""
    await _add_number(api_client)
    created = await api_client.post(
        "/api/v1/sessions", json={"party_a": A, "party_b": B}
    )
    await api_client.delete(f"/api/v1/sessions/{created.json()['session_id']}")

    response = await api_client.get("/api/v1/numbers")

    body = response.json()
    assert body["total"] == 1
    assert body["in_cooldown"] == 1
    assert body["free"] == 0
    assert body["numbers"][0]["in_cooldown"] is True


async def test_duplicate_number_is_409(api_client) -> None:
    await _add_number(api_client)
    response = await api_client.post(
        "/api/v1/numbers", json={"e164": "+77172000101"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "number_already_exists"


async def test_health_and_metrics_need_no_api_key(api_client) -> None:
    health = await api_client.get("/health", headers={"X-API-Key": ""})
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    metrics = await api_client.get("/metrics", headers={"X-API-Key": ""})
    assert metrics.status_code == 200
    assert "masking_sessions_active" in metrics.text


async def test_trace_id_is_echoed(api_client) -> None:
    response = await api_client.get(
        "/health", headers={"X-Trace-Id": "trace-abc"}
    )
    assert response.headers["X-Trace-Id"] == "trace-abc"


async def test_metrics_expose_every_metric_of_the_specification(
    api_client,
) -> None:
    """Every metric family the service is expected to expose is present."""
    await _add_number(api_client)
    await api_client.post("/api/v1/sessions", json={"party_a": A, "party_b": B})

    body = (await api_client.get("/metrics", headers={"X-API-Key": ""})).text

    for family in (
        "masking_sessions_active",
        "masking_numbers_free",
        "masking_calls_total",
        "masking_call_setup_duration_seconds",
        "masking_ari_ws_connected",
    ):
        assert f"# TYPE {family}" in body, family
    assert "masking_call_setup_duration_seconds_count 0" in body
