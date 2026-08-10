"""Cryptography for personal data.

Two independent primitives with two independent keys:

* :func:`party_hash` — HMAC-SHA256 of a phone number. Deterministic, so it can
  be indexed and used for the ``(proxy, caller)`` lookup. Being a keyed hash
  rather than a plain digest, a leaked database cannot be brute-forced against
  the small phone number space without the secret.
* :func:`encrypt_e164` and :func:`decrypt_e164` — AES-256-GCM with a random
  nonce. Real numbers are only ever stored as ciphertext.

Encryption happens in the application rather than in the database, which
keeps the key off the database server, out of ``pg_stat_activity`` and out of
query logs. The storage shape (``BYTEA``) is the same either way, so moving the
work into ``pgcrypto`` later would be a data migration only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers import aead

NONCE_BYTES = 12


def party_hash(e164: str, secret: str) -> str:
    """Returns the searchable keyed hash of a phone number.

    Args:
        e164: Phone number in strict E.164 form.
        secret: HMAC key, from ``PARTY_HASH_SECRET``.

    Returns:
        The HMAC-SHA256 digest as a 64-character hex string.
    """
    return hmac.new(
        secret.encode("utf-8"), e164.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def encrypt_e164(e164: str, key: bytes) -> bytes:
    """Encrypts a phone number with AES-256-GCM.

    Args:
        e164: Phone number in strict E.164 form.
        key: 32-byte AES key.

    Returns:
        The ciphertext laid out as ``nonce(12) || ciphertext || tag(16)``.
    """
    nonce = os.urandom(NONCE_BYTES)
    return nonce + aead.AESGCM(key).encrypt(nonce, e164.encode("utf-8"), None)


def decrypt_e164(blob: bytes | memoryview, key: bytes) -> str:
    """Decrypts a phone number produced by :func:`encrypt_e164`.

    Args:
        blob: Ciphertext as stored in the database.
        key: The same 32-byte AES key used to encrypt.

    Returns:
        The phone number in strict E.164 form.

    Raises:
        ValueError: The ciphertext is too short to contain a nonce.
        cryptography.exceptions.InvalidTag: The key is wrong or the ciphertext
            was tampered with.
    """
    data = bytes(blob)
    if len(data) <= NONCE_BYTES:
        raise ValueError(f"ciphertext too short: {len(data)=}")
    nonce, payload = data[:NONCE_BYTES], data[NONCE_BYTES:]
    return aead.AESGCM(key).decrypt(nonce, payload, None).decode("utf-8")


def generate_ext_code(length: int) -> str:
    """Returns a cryptographically strong numeric PIN, zero-padded.

    Args:
        length: Number of digits, must be at least 1.

    Returns:
        The PIN, for example ``"0417"``.

    Raises:
        ValueError: The requested length is not positive.
    """
    if length < 1:
        raise ValueError(f"ext code length must be >= 1, got {length=}")
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


def webhook_signature(body: bytes, secret: str) -> str:
    """Returns the ``sha256=<hex>`` HMAC over a raw webhook body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def constant_time_equals(left: str, right: str) -> bool:
    """Compares two strings without leaking their contents through timing."""
    return hmac.compare_digest(left, right)
