"""Domain errors and their HTTP mapping.

Every error the API layer knows how to turn into a response derives from
:class:`DomainError`. The ``code`` attribute is the stable machine-readable
identifier returned to clients; the HTTP status is an implementation detail of
the transport and lives next to it so the two cannot drift apart.
"""

from __future__ import annotations


class DomainError(Exception):
    """A failure that the API can report to the client verbatim.

    Attributes:
        status_code: HTTP status the API layer responds with.
        code: Stable machine-readable identifier, part of the public API.
        message: Human-readable explanation, scrubbed before it is sent.
    """

    status_code = 400
    code = "bad_request"

    def __init__(
        self, message: str | None = None, *, code: str | None = None
    ) -> None:
        """Initializes the error.

        Args:
            message: Human-readable explanation. Defaults to ``code``.
            code: Overrides the class-level error code for this instance.
        """
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code


class ValidationError(DomainError):
    """A request field failed validation."""

    status_code = 422
    code = "validation_failed"


class NoNumberAvailableError(DomainError):
    """No proxy number can be allocated to the requested pair."""

    status_code = 409
    code = "no_number_available"


class SessionNotFoundError(DomainError):
    """The requested session does not exist."""

    status_code = 404
    code = "session_not_found"


class NumberNotFoundError(DomainError):
    """The requested number is not in the pool."""

    status_code = 404
    code = "number_not_found"


class NumberAlreadyExistsError(DomainError):
    """The number is already in the pool."""

    status_code = 409
    code = "number_already_exists"


class SessionNotActiveError(DomainError):
    """The session is closed or expired and cannot be modified."""

    status_code = 409
    code = "session_not_active"


class UnauthorizedError(DomainError):
    """The API key is missing or unknown."""

    status_code = 401
    code = "unauthorized"


class RateLimitedError(DomainError):
    """The API key exceeded its request quota."""

    status_code = 429
    code = "rate_limited"


class PayloadTooLargeError(DomainError):
    """The request body exceeds the configured limit."""

    status_code = 413
    code = "payload_too_large"
