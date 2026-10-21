"""FYERS credentials from environment variables or a local ``.env`` file.

Precedence: real environment variables win over ``.env`` values. The ``.env`` file
is parsed into a private dict; it is never copied into ``os.environ`` and only the
two ``FYERS_*`` names below are read from it. Values are never printed or logged:
``repr``/``str`` of :class:`Credentials` are masked, and :meth:`Credentials.redact`
scrubs them from any text (API messages, raw responses) before it is shown or saved.

The downloader needs only ``FYERS_APP_ID`` and ``FYERS_ACCESS_TOKEN``; it never reads
``FYERS_APP_SECRET``. The local login helper (``fyers.login``) additionally needs
``FYERS_APP_ID`` and ``FYERS_APP_SECRET`` (see :class:`AuthCredentials`) and then
writes only ``FYERS_ACCESS_TOKEN`` back to ``.env``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import CredentialsError, MissingCredentialsError

logger = logging.getLogger(__name__)

APP_ID_VAR = "FYERS_APP_ID"
ACCESS_TOKEN_VAR = "FYERS_ACCESS_TOKEN"
APP_SECRET_VAR = "FYERS_APP_SECRET"
REDACTED = "***"

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


@dataclass(frozen=True, repr=False)
class Credentials:
    app_id: str
    access_token: str

    def __post_init__(self) -> None:
        for name, value in (("app id", self.app_id), ("access token", self.access_token)):
            if not value:
                raise CredentialsError(f"{name} is empty")
            if _BAD_CHARS.search(value):  # also blocks header injection via newlines
                raise CredentialsError(f"{name} contains whitespace or control characters")

    def __repr__(self) -> str:
        return f"Credentials(app_id='{REDACTED}', access_token='{REDACTED}')"

    @property
    def authorization(self) -> str:
        """Value of the ``Authorization`` header: ``<app_id>:<access_token>`` (FYERS v3)."""
        return f"{self.app_id}:{self.access_token}"

    def redact(self, text: str) -> str:
        """Remove the token and app id from ``text``."""
        for secret in (self.access_token, self.app_id):
            text = text.replace(secret, REDACTED)
        return text


class SecretStr:
    """Holds a secret (auth code, access token) so it cannot be printed or logged by accident.

    ``repr``/``str``/f-strings show ``***``; call :meth:`reveal` only where the value
    must actually be used (the token exchange request, the ``.env`` write).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretStr('{REDACTED}')"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self):
        raise TypeError("SecretStr cannot be pickled")


@dataclass(frozen=True, repr=False)
class AuthCredentials:
    """App id and app secret for the login helper only (never used by the downloader)."""

    app_id: str
    app_secret: str

    def __post_init__(self) -> None:
        for name, value in (("app id", self.app_id), ("app secret", self.app_secret)):
            if not value:
                raise CredentialsError(f"{name} is empty")
            if _BAD_CHARS.search(value):
                raise CredentialsError(f"{name} contains whitespace or control characters")

    def __repr__(self) -> str:
        return f"AuthCredentials(app_id='{REDACTED}', app_secret='{REDACTED}')"

    @property
    def app_id_hash(self) -> str:
        """``SHA256(app_id:app_secret)`` as lowercase hex, as FYERS v3 ``validate-authcode`` requires."""
        return hashlib.sha256(f"{self.app_id}:{self.app_secret}".encode()).hexdigest()

    def redact(self, text: str) -> str:
        """Remove the secret, the derived hash and the app id from ``text``."""
        for secret in (self.app_secret, self.app_id_hash, self.app_id):
            text = text.replace(secret, REDACTED)
        return text


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (``#`` comments, ``export`` prefix, quotes allowed).

    Errors name the line number only, never the line content.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, separator, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not _KEY_RE.fullmatch(key):
            raise CredentialsError(f".env line {number} is not KEY=VALUE")
        if value.startswith(('"', "'")):
            end = value.find(value[0], 1)
            if end == -1:
                raise CredentialsError(f".env line {number} has an unterminated quote")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def _warn_if_readable_by_others(path: Path) -> None:
    if os.name != "posix":
        return
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning("%s is accessible by other users; run: chmod 600 %s", path, path)


def _source_lookup(env: Mapping[str, str] | None, dotenv_path: str | Path) -> Callable[[str], str | None]:
    """Return ``lookup(name)``: real environment first, then the ``.env`` file."""
    env = os.environ if env is None else env
    file_values: dict[str, str] = {}
    path = Path(dotenv_path)
    if path.is_file():
        _warn_if_readable_by_others(path)
        try:
            file_values = parse_dotenv(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CredentialsError(f"cannot read {path}: {type(exc).__name__}") from None

    def lookup(name: str) -> str | None:
        value = (env.get(name) or "").strip() or file_values.get(name, "").strip()
        return value or None

    return lookup


def _require(names_values: tuple[tuple[str, str | None], ...]) -> None:
    missing = [name for name, value in names_values if not value]
    if missing:
        raise MissingCredentialsError(
            f"missing {', '.join(missing)}: set it as an environment variable or in a local, "
            "git-ignored .env file (see README)"
        )


def load_credentials(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | Path = ".env",
) -> Credentials:
    """Load downloader credentials from ``env`` (default ``os.environ``), falling back to ``.env``."""
    lookup = _source_lookup(env, dotenv_path)
    app_id, token = lookup(APP_ID_VAR), lookup(ACCESS_TOKEN_VAR)
    _require(((APP_ID_VAR, app_id), (ACCESS_TOKEN_VAR, token)))
    return Credentials(app_id=app_id, access_token=token)


def load_auth_credentials(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | Path = ".env",
) -> AuthCredentials:
    """Load the app id and app secret for the login helper (environment first, then ``.env``)."""
    lookup = _source_lookup(env, dotenv_path)
    app_id, secret = lookup(APP_ID_VAR), lookup(APP_SECRET_VAR)
    _require(((APP_ID_VAR, app_id), (APP_SECRET_VAR, secret)))
    return AuthCredentials(app_id=app_id, app_secret=secret)
