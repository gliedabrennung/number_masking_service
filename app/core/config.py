"""Application configuration, read from environment variables.

Typical usage example:

    settings = config.get_settings()
    ttl = settings.default_ttl_seconds
"""

from __future__ import annotations

import base64
import functools
import hashlib
from typing import Literal

import pydantic
import pydantic_settings

_MIN_HASH_SECRET_LENGTH = 32
_AES_KEY_BYTES = 32


class Settings(pydantic_settings.BaseSettings):
    """Every knob of the service, with production-safe defaults.

    Values come from the process environment and, when present, from a ``.env``
    file. Secrets have deliberately weak placeholder defaults so the service
    starts in development; :meth:`validate_production_secrets` reports them.
    """

    model_config = pydantic_settings.SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = (
        "postgresql+asyncpg://masking:masking@127.0.0.1:5432/masking"
    )
    redis_url: str = "redis://127.0.0.1:6379/0"

    ari_url: str = "http://127.0.0.1:8088"
    ari_user: str = "masking"
    ari_password: str = "masking"
    ari_app: str = "masking"
    endpoint_template: str = "PJSIP/{digits}"
    ari_app_check_seconds: float = 15.0
    ari_reconnect_min_seconds: float = 1.0
    ari_reconnect_max_seconds: float = 30.0

    api_keys: str = "dev-key-change-me"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_rate_limit_per_minute: int = 60
    api_max_body_bytes: int = 8192
    run_ari_in_api: bool = True

    default_ttl_seconds: int = 86_400
    max_ttl_seconds: int = 2_592_000
    number_cooldown_hours: int = 24
    cooldown_scope: Literal["number", "party"] = "number"
    originate_timeout: int = 45
    ext_code_mode: Literal["auto", "always", "never"] = "auto"
    ext_code_length: int = 4
    ext_code_max_per_number: int = 50
    dtmf_digit_timeout_seconds: float = 5.0
    dtmf_total_timeout_seconds: float = 15.0
    dtmf_max_attempts: int = 3

    call_retention_days: int = 90
    closed_session_retention_days: int = 30
    cleanup_interval_seconds: int = 3600
    expiry_scan_interval_seconds: int = 30

    party_hash_secret: str = "dev-hash-secret-change-me-32-chars-min"
    encryption_key: str = pydantic.Field(default="")

    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_max_attempts: int = 5
    webhook_timeout_seconds: float = 5.0

    log_level: str = "INFO"
    log_json: bool = True

    sound_expired: str = "custom/session-expired"
    sound_unknown: str = "custom/number-unavailable"
    sound_enter_code: str = "custom/enter-code"
    sound_wrong_code: str = "custom/wrong-code"
    sound_error: str = "custom/tech-error"

    @pydantic.field_validator("cooldown_scope", "ext_code_mode", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        """Accepts enum-like settings in any case."""
        return value.lower() if isinstance(value, str) else value

    @property
    def api_key_set(self) -> frozenset[str]:
        """The accepted ``X-API-Key`` values."""
        return frozenset(
            key.strip() for key in self.api_keys.split(",") if key.strip()
        )

    @property
    def encryption_key_bytes(self) -> bytes:
        """The AES-256-GCM key material.

        Accepts base64 of exactly 32 bytes. When unset — development only — a
        deterministic key is derived from the HMAC secret so the service still
        starts; :meth:`validate_production_secrets` flags that case.

        Returns:
            The 32-byte key.

        Raises:
            ValueError: The configured value is not base64 of 32 bytes.
        """
        raw = self.encryption_key.strip()
        if not raw:
            return hashlib.sha256(self.party_hash_secret.encode()).digest()
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be base64-encoded 32 bytes"
            ) from exc
        if len(key) != _AES_KEY_BYTES:
            raise ValueError(
                f"ENCRYPTION_KEY must decode to exactly {_AES_KEY_BYTES} bytes,"
                f" got {len(key)}"
            )
        return key

    @property
    def ari_ws_url(self) -> str:
        """The ARI websocket URL derived from :attr:`ari_url`."""
        base = self.ari_url.rstrip("/")
        scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[1]
        return f"{scheme}://{host}/ari/events"

    @property
    def webhooks_enabled(self) -> bool:
        """True when both a webhook URL and a signing secret are configured."""
        return bool(self.webhook_url and self.webhook_secret)

    def validate_production_secrets(self) -> list[str]:
        """Returns a warning for every secret left at an insecure default.

        Never raises: the service must still start in development, it just
        says so loudly in the log.
        """
        problems: list[str] = []
        if (
            "change-me" in self.party_hash_secret
            or len(self.party_hash_secret) < _MIN_HASH_SECRET_LENGTH
        ):
            problems.append(
                "PARTY_HASH_SECRET is a default or shorter than "
                f"{_MIN_HASH_SECRET_LENGTH} chars"
            )
        raw_key = self.encryption_key
        if not raw_key.strip() or "change-me" in raw_key:
            problems.append("ENCRYPTION_KEY is unset or a default value")
        if any(
            "change-me" in key or key == "dev-key-change-me"
            for key in self.api_key_set
        ):
            problems.append("API_KEYS contains a default value")
        if "change-me" in self.ari_password:
            problems.append("ARI_PASSWORD is a default value")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    """Returns the process-wide settings, parsed once."""
    return Settings()
