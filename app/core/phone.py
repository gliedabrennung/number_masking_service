"""E.164 handling and masking of phone numbers.

Every phone number entering the service passes through :func:`normalize_e164`,
and every phone number leaving it towards a human — an API response or a log
line — passes through :func:`mask_e164`. A full number is never emitted.

Typical usage example:

    number = phone.normalize_e164("8 701 123-45-67")
    log.info("call.inbound", caller_masked=phone.mask_e164(number))
"""

from __future__ import annotations

import re

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

_LOOSE_NUMBER_RE = re.compile(r"\+?\d{7,15}")

_DEFAULT_COUNTRY_PREFIX = "7"
_NATIONAL_NUMBER_LENGTH = 11


class InvalidPhoneNumberError(ValueError):
    """A string cannot be interpreted as an E.164 phone number."""


def normalize_e164(
    value: str, *, default_country_prefix: str | None = None
) -> str:
    """Normalizes a user-supplied number to strict E.164.

    Accepts the local notations common in KZ and RU input (``8XXXXXXXXXX``,
    spaces, dashes, parentheses, an international ``00`` prefix) and returns
    the ``+7...`` form.

    Args:
        value: Number as supplied by the caller.
        default_country_prefix: Digits, without ``+``, prepended to a national
            number that starts with the trunk code ``8``. Defaults to ``7``.

    Returns:
        The number in strict E.164 form, for example ``+77011234567``.

    Raises:
        InvalidPhoneNumberError: The value holds no digits, or the result is
            not a valid E.164 number. The message contains only a masked form
            of the input.
    """
    if not isinstance(value, str):
        raise InvalidPhoneNumberError("phone number must be a string")

    raw = value.strip()
    if not raw:
        raise InvalidPhoneNumberError("phone number is empty")

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise InvalidPhoneNumberError(f"no digits in {mask_e164(raw)}")

    if not has_plus:
        prefix = default_country_prefix or _DEFAULT_COUNTRY_PREFIX
        if digits.startswith("8") and len(digits) == _NATIONAL_NUMBER_LENGTH:
            digits = prefix + digits[1:]
        elif digits.startswith("00"):
            digits = digits[2:]

    candidate = "+" + digits
    if not E164_RE.match(candidate):
        raise InvalidPhoneNumberError(
            f"{mask_e164(candidate)} is not a valid E.164 number"
        )
    return candidate


def is_e164(value: str) -> bool:
    """Returns True when the value is already in strict E.164 form."""
    return bool(isinstance(value, str) and E164_RE.match(value))


def mask_e164(value: str | None) -> str:
    """Masks a phone number for display: ``+77011234567`` -> ``+7701***4567``.

    Args:
        value: Number to mask, or None.

    Returns:
        The masked number, or an empty string for an empty input. Values too
        short to mask in the usual shape keep only their last two characters,
        so no partial leak happens on unexpected input.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= 6:
        return "*" * max(len(text) - 2, 0) + text[-2:]
    return f"{text[:5]}***{text[-4:]}"


def scrub_text(text: str) -> str:
    """Returns the text with every phone-number-shaped digit run masked."""
    return _LOOSE_NUMBER_RE.sub(lambda match: mask_e164(match.group(0)), text)
