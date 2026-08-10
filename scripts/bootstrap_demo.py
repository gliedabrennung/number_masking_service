#!/usr/bin/env python3
"""Seeds the demo stand and prints the softphone settings.

Run through ``make demo``, after the containers are up and the migrations have
been applied. Idempotent: a second run only adds what is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import sqlalchemy as sa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import config, logging_config  # noqa: E402
from app.db import engine, models  # noqa: E402
from app.services import numbers as numbers_service  # noqa: E402

DEMO_NUMBERS = ["+77172000101", "+77172000102", "+77172000103"]
_SEPARATOR = "=" * 72


async def seed(numbers: list[str]) -> list[str]:
    """Adds the demo numbers that are not in the pool yet.

    Args:
        numbers: Numbers the demo stand should serve.

    Returns:
        The numbers that this run actually inserted.
    """
    added: list[str] = []
    async with engine.session_scope() as db:
        for e164 in numbers:
            exists = await db.scalar(
                sa.select(models.Number.id).where(models.Number.e164 == e164)
            )
            if exists:
                continue
            await numbers_service.add_number(db, e164=e164, provider="demo")
            added.append(e164)
    return added


def print_softphone_help(settings: config.Settings) -> None:
    """Prints ready-to-paste SIP settings and a session-creating curl call."""
    host = os.environ.get("DEMO_SIP_HOST", "<IP-адрес хоста со стендом>")
    accounts = [
        (
            "A",
            os.environ.get("SIP_A_USER", "77011234567"),
            os.environ.get("SIP_A_PASSWORD", ""),
        ),
        (
            "B",
            os.environ.get("SIP_B_USER", "77019876543"),
            os.environ.get("SIP_B_PASSWORD", ""),
        ),
        (
            "C",
            os.environ.get("SIP_C_USER", "77015550022"),
            os.environ.get("SIP_C_PASSWORD", ""),
        ),
    ]

    print()
    print(_SEPARATOR)
    print("Демо-стенд готов")
    print(_SEPARATOR)
    print()
    print("SIP-аккаунты (Zoiper / Linphone / MicroSIP):")
    for label, user, password in accounts:
        if not password:
            continue
        print(f"  Абонент {label}:")
        print(f"    domain / server : {host}")
        print(f"    username        : {user}")
        print(f"    password        : {password}")
        print("    transport       : UDP, порт 5060")
        print()
    print("Пул прокси-номеров:", ", ".join(DEMO_NUMBERS))
    print()
    print("Создать сессию:")
    api_key = next(iter(sorted(settings.api_key_set)), "<API_KEY>")
    body = (
        f'{{"party_a":"+{accounts[0][1]}",'
        f'"party_b":"+{accounts[1][1]}","ttl_seconds":3600}}'
    )
    print(
        f"  curl -sS -X POST"
        f" http://127.0.0.1:{settings.api_port}/api/v1/sessions \\\n"
        f"    -H 'Content-Type: application/json' \\\n"
        f"    -H 'X-API-Key: {api_key}' \\\n"
        f"    -d '{body}'"
    )
    print()
    print("Затем набрать выданный proxy_number с софтфона A. На экране B")
    print("должен отобразиться прокси-номер, а не номер A.")
    print(_SEPARATOR)


async def run(numbers: list[str], quiet: bool) -> int:
    """Seeds the pool and prints the instructions.

    Args:
        numbers: Numbers the demo stand should serve.
        quiet: Suppress the printed instructions.

    Returns:
        The process exit code.
    """
    settings = config.get_settings()
    logging_config.configure_logging(settings.log_level, json_output=False)
    engine.init_engine(settings)
    try:
        added = await seed(numbers)
    finally:
        await engine.dispose_engine()

    if not quiet:
        print(
            f"pool: {len(added)} number(s) added,"
            f" {len(numbers)} total in the demo set"
        )
        print_softphone_help(settings)
    return 0


def main() -> int:
    """Parses the command line and runs the bootstrap."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numbers", nargs="*", default=DEMO_NUMBERS)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.numbers, args.quiet))


if __name__ == "__main__":
    sys.exit(main())
