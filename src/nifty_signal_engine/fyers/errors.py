"""Exceptions raised by the FYERS downloader. Messages never contain credentials."""

from __future__ import annotations


class FyersError(Exception):
    """Base class for downloader errors."""


class CredentialsError(FyersError):
    """Credentials are malformed or could not be read."""


class MissingCredentialsError(CredentialsError):
    """A required credential variable is not set."""


class FyersNetworkError(FyersError):
    """The request failed before an HTTP response was received (DNS, TLS, timeout)."""


class FyersApiError(FyersError):
    """FYERS (or the connection) returned an error response."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class FyersAuthError(FyersApiError):
    """The app id / access token was rejected. Regenerate the access token."""


class FyersRateLimitError(FyersApiError):
    """Rate limit still exceeded after all retries."""


class AuthFlowError(FyersError):
    """The browser login / callback step failed (invalid state, error callback, timeout, port busy)."""


class EnvFileError(FyersError):
    """The local .env file cannot be updated safely (symlink, wrong type or owner, not git-ignored)."""


class NoDataError(FyersError):
    """FYERS returned no candles for the whole requested range."""
