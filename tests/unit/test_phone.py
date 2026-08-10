from __future__ import annotations

import pytest

from app.core import phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+77011234567", "+77011234567"),
        (" +7 701 123-45-67 ", "+77011234567"),
        ("+7 (701) 123 45 67", "+77011234567"),
        ("87011234567", "+77011234567"),
        ("0077011234567", "+77011234567"),
        ("+442071838750", "+442071838750"),
    ],
)
def test_normalize_accepts_common_notations(raw: str, expected: str) -> None:
    assert phone.normalize_e164(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "abc", "+0123456789", "12345", "+" + "9" * 16]
)
def test_normalize_rejects_invalid(raw: str) -> None:
    with pytest.raises(phone.InvalidPhoneNumberError):
        phone.normalize_e164(raw)


def test_normalize_error_message_never_contains_the_full_number() -> None:
    with pytest.raises(phone.InvalidPhoneNumberError) as caught:
        phone.normalize_e164("+0123456789")
    assert "0123456789" not in str(caught.value)


def test_is_e164() -> None:
    assert phone.is_e164("+77011234567")
    assert not phone.is_e164("77011234567")
    assert not phone.is_e164("+0" + "1" * 9)


def test_mask_keeps_prefix_and_last_four() -> None:
    assert phone.mask_e164("+77011234567") == "+7701***4567"
    assert phone.mask_e164(None) == ""
    assert phone.mask_e164("+7701") == "***01"


def test_scrub_text_masks_every_number_in_free_text() -> None:
    text = "call from +77011234567 to 77019876543 failed"
    scrubbed = phone.scrub_text(text)
    assert "77011234567" not in scrubbed
    assert "77019876543" not in scrubbed
    assert "failed" in scrubbed


def test_scrub_text_leaves_short_digit_runs_alone() -> None:
    text = "attempt 3 of 5, code 12345"
    assert phone.scrub_text(text) == text
