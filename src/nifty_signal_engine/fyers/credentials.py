"""FYERS credentials from environment variables or a local ``.env`` file.

Precedence: real environment variables win over ``.env`` values. The ``.env`` file
is parsed into a private dict; it is never copied into ``os.environ`` and only the
two ``FYERS_*`` names below are read from it. Values are never printed or logged:
``repr``/``str`` of :class:`Credentials` are masked, and :meth:`Credentials.redact`
scrubs them from any text (API messages, raw responses) before it is shown or saved.

``FYERS_SECRET_KEY`` is intentionally not used: the downloader only needs an
already-generated access token, so the app secret never has to be on this machine's
environment for downloads.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import CredentialsError, MissingCredentialsError

logger = logging.getLogger(__name__)

APP_ID_VAR = "FYERS_APP_ID"
ACCESS_TOKEN_VAR = "FYERS_ACCESS_TOKEN"
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


def load_credentials(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | Path = ".env",
) -> Credentials:
    """Load credentials from ``env`` (default ``os.environ``), falling back to ``.env``."""
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

    app_id, token = lookup(APP_ID_VAR), lookup(ACCESS_TOKEN_VAR)
    missing = [n for n, v in ((APP_ID_VAR, app_id), (ACCESS_TOKEN_VAR, token)) if not v]
    if missing:
        raise MissingCredentialsError(
            f"missing {', '.join(missing)}: set it as an environment variable or in a local, "
            "git-ignored .env file (see README)"
        )
    return Credentials(app_id=app_id, access_token=token)
