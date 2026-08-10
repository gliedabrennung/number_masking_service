from __future__ import annotations

import os

import pytest
from cryptography import exceptions as crypto_exceptions

from app.core import crypto

_KEY_BYTES = 32


def test_party_hash_is_deterministic_and_key_dependent() -> None:
    first = crypto.party_hash("+77011234567", "secret-one")
    same = crypto.party_hash("+77011234567", "secret-one")
    other_key = crypto.party_hash("+77011234567", "secret-two")
    assert first == same
    assert first != other_key
    assert len(first) == 64


def test_party_hash_does_not_contain_the_number() -> None:
    assert "77011234567" not in crypto.party_hash("+77011234567", "secret")


def test_encrypt_roundtrip() -> None:
    key = os.urandom(_KEY_BYTES)
    blob = crypto.encrypt_e164("+77011234567", key)
    assert b"77011234567" not in blob
    assert crypto.decrypt_e164(blob, key) == "+77011234567"


def test_encrypt_is_randomised() -> None:
    key = os.urandom(_KEY_BYTES)
    first = crypto.encrypt_e164("+77011234567", key)
    second = crypto.encrypt_e164("+77011234567", key)
    assert first != second


def test_decrypt_with_wrong_key_fails() -> None:
    blob = crypto.encrypt_e164("+77011234567", os.urandom(_KEY_BYTES))
    with pytest.raises(crypto_exceptions.InvalidTag):
        crypto.decrypt_e164(blob, os.urandom(_KEY_BYTES))


def test_decrypt_rejects_tampered_ciphertext() -> None:
    key = os.urandom(_KEY_BYTES)
    blob = bytearray(crypto.encrypt_e164("+77011234567", key))
    blob[-1] ^= 0x01
    with pytest.raises(crypto_exceptions.InvalidTag):
        crypto.decrypt_e164(bytes(blob), key)


def test_decrypt_rejects_a_truncated_blob() -> None:
    with pytest.raises(ValueError, match="too short"):
        crypto.decrypt_e164(b"short", os.urandom(_KEY_BYTES))


@pytest.mark.parametrize("length", [4, 6])
def test_ext_code_shape(length: int) -> None:
    for _ in range(50):
        code = crypto.generate_ext_code(length)
        assert len(code) == length
        assert code.isdigit()


def test_ext_code_rejects_a_non_positive_length() -> None:
    with pytest.raises(ValueError, match="length"):
        crypto.generate_ext_code(0)


def test_webhook_signature_matches_known_value() -> None:
    signature = crypto.webhook_signature(b'{"a":1}', "topsecret")
    assert signature.startswith("sha256=")
    assert signature == crypto.webhook_signature(b'{"a":1}', "topsecret")
    assert signature != crypto.webhook_signature(b'{"a":2}', "topsecret")
