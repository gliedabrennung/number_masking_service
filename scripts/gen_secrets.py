#!/usr/bin/env python3
"""Creates ``.env`` from ``.env.example`` with freshly generated secrets.

Refuses to overwrite an existing ``.env``: rotating live secrets must be a
deliberate act, not a side effect of running make.
"""

from __future__ import annotations

import base64
import os
import pathlib
import re
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"

DEMO_SIP_USERS = {
    "SIP_A_USER": "77011234567",
    "SIP_B_USER": "77019876543",
    "SIP_C_USER": "77015550022",
}

_AES_KEY_BYTES = 32
_ENV_FILE_MODE = 0o600


def _generated_values() -> dict[str, str]:
    """Returns every secret the stand needs, freshly generated."""
    postgres_password = secrets.token_urlsafe(24)
    return {
        "POSTGRES_PASSWORD": postgres_password,
        "ARI_PASSWORD": secrets.token_urlsafe(24),
        "API_KEYS": secrets.token_urlsafe(32),
        "PARTY_HASH_SECRET": secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": base64.b64encode(os.urandom(_AES_KEY_BYTES)).decode(),
        "WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "SIP_A_PASSWORD": secrets.token_urlsafe(20),
        "SIP_B_PASSWORD": secrets.token_urlsafe(20),
        "SIP_C_PASSWORD": secrets.token_urlsafe(20),
        "DATABASE_URL": (
            f"postgresql+asyncpg://masking:{postgres_password}"
            "@127.0.0.1:5432/masking"
        ),
    }


def main() -> int:
    """Writes ``.env`` and returns the process exit code."""
    if TARGET.exists():
        print(
            f"{TARGET} already exists — remove it first if you really want"
            " new secrets"
        )
        return 1

    values = _generated_values()
    text = EXAMPLE.read_text(encoding="utf-8")
    for key, value in values.items():
        text, count = re.subn(rf"(?m)^{key}=.*$", f"{key}={value}", text)
        if count == 0:
            text += f"\n{key}={value}\n"

    text += (
        "\n# Demo SIP accounts (endpoints are named after their own digits)\n"
    )
    for key, value in DEMO_SIP_USERS.items():
        text += f"{key}={value}\n"

    TARGET.write_text(text, encoding="utf-8")
    TARGET.chmod(_ENV_FILE_MODE)
    print(f"wrote {TARGET} with generated secrets (mode 0600)")
    print("API key:", values["API_KEYS"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
