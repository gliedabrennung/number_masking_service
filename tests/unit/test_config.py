from __future__ import annotations

import base64
import os

import pytest

from app.core import config

_EXPECTED_DEFAULT_PROBLEMS = 4


def _settings(**kwargs: object) -> config.Settings:
    base = {
        "party_hash_secret": "x" * 40,
        "encryption_key": base64.b64encode(os.urandom(32)).decode(),
        "api_keys": "key-one,key-two",
        "ari_password": "s3cret-ari-password",
        "webhook_url": "",
        "webhook_secret": "",
    }
    base.update(kwargs)
    return config.Settings(_env_file=None, **base)


def test_api_keys_are_split_and_trimmed() -> None:
    assert _settings().api_key_set == frozenset({"key-one", "key-two"})


def test_encryption_key_must_be_32_bytes() -> None:
    settings = _settings(encryption_key=base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32 bytes"):
        _ = settings.encryption_key_bytes


def test_missing_encryption_key_derives_a_development_key() -> None:
    settings = _settings(encryption_key="")
    assert len(settings.encryption_key_bytes) == 32
    problems = settings.validate_production_secrets()
    assert "ENCRYPTION_KEY is unset or a default value" in problems


def test_insecure_defaults_are_reported() -> None:
    settings = config.Settings(
        _env_file=None,
        party_hash_secret="change-me-hash-secret",
        encryption_key="change-me-base64-32-bytes",
        api_keys="dev-key-change-me",
        ari_password="change-me-ari",
    )
    problems = settings.validate_production_secrets()
    assert len(problems) == _EXPECTED_DEFAULT_PROBLEMS


def test_clean_configuration_reports_nothing() -> None:
    assert _settings().validate_production_secrets() == []


def test_ari_ws_url_derivation() -> None:
    plain = _settings(ari_url="http://127.0.0.1:8088")
    secure = _settings(ari_url="https://ari.internal")
    assert plain.ari_ws_url == "ws://127.0.0.1:8088/ari/events"
    assert secure.ari_ws_url == "wss://ari.internal/ari/events"


def test_webhooks_need_both_url_and_secret() -> None:
    url_only = _settings(webhook_url="https://example.test/hook")
    both = _settings(
        webhook_url="https://example.test/hook", webhook_secret="s"
    )
    assert not url_only.webhooks_enabled
    assert both.webhooks_enabled
