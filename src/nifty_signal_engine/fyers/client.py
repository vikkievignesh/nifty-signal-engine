"""Minimal read-only client for the FYERS API v3 history endpoint.

Endpoint, headers and parameters follow the official ``fyers-apiv3`` Python SDK
(3.2.0): ``GET https://api-t1.fyers.in/data/history`` with the header
``Authorization: <app_id>:<access_token>`` and ``version: 3``. Parameters:
``symbol``, ``resolution``, ``date_format`` (0 = epoch seconds), ``range_from``,
``range_to``, ``cont_flag``.

Behaviour
---------
* Requests are paced (``min_interval_seconds``) and retried with exponential
  backoff on HTTP 429, HTTP 5xx and network errors. Authentication failures and
  other API errors are never retried.
* Rate-limit and error-code handling is based on HTTP status plus the ``s`` /
  ``code`` / ``message`` fields. The exact FYERS error-code table and numeric rate
  limits could not be read when this was written, so the pacing defaults are
  deliberately conservative and the code lists below are best-effort.
* The transport is injectable so tests run with no network. Credentials appear only
  in the ``Authorization`` header and are scrubbed from every message, log line and
  exception this module produces.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .credentials import Credentials
from .errors import FyersApiError, FyersAuthError, FyersNetworkError, FyersRateLimitError
from .netdiag import PERMANENT_CATEGORIES, network_error_from

logger = logging.getLogger(__name__)

BASE_URL = "https://api-t1.fyers.in/data"
HISTORY_PATH = "/history"
MAX_BACKOFF_SECONDS = 60.0
_MAX_MESSAGE_CHARS = 300

# Best-effort classification; see module docstring.
_AUTH_CODES = frozenset({401, 403, -8, -15, -16, -17})
_RATE_LIMIT_CODES = frozenset({429, -429})
_AUTH_WORDS = ("token", "authenticat", "unauthor")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str], float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward credential-bearing headers to the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Default transport: one HTTPS GET using the standard library (redirects are not followed)."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310 (https enforced)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return HttpResponse(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:  # non-2xx: still an HTTP response
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise network_error_from(exc) from None  # classified, fixed wording; never str(exc)


class _Retry(Exception):
    """Internal: a retryable failure carrying the error to raise if retries run out."""

    def __init__(self, error: FyersApiError | FyersNetworkError, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.error = error
        self.reason = reason
        self.retry_after = retry_after


class FyersHistoryClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        min_interval_seconds: float = 1.0,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("base_url must be https:// so the access token is never sent in clear text")
        if max_retries < 0 or backoff_seconds < 0 or min_interval_seconds < 0 or timeout <= 0:
            raise ValueError("retry, backoff, interval and timeout settings must be non-negative (timeout > 0)")
        self._credentials = credentials
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._min_interval = min_interval_seconds
        self._last_request_at: float | None = None

    # -- public -------------------------------------------------------------------------

    def fetch_history(self, symbol: str, resolution: str, range_from: int, range_to: int) -> dict[str, Any]:
        """Return the parsed JSON payload for one history request (epoch-second range).

        The payload is returned as FYERS sent it (``s``, ``code``, ``message``,
        ``candles``); an empty window yields a payload with no/empty ``candles``.
        """
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": 0,
            "range_from": int(range_from),
            "range_to": int(range_to),
            "cont_flag": 1,
        }
        url = f"{self._base_url}{HISTORY_PATH}?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": self._credentials.authorization,
            "Content-Type": "application/json",
            "version": "3",
        }
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                return self._attempt(url, headers)
            except _Retry as retry:
                if attempt == self._max_retries:
                    raise retry.error from None
                delay = retry.retry_after if retry.retry_after is not None else self._backoff * 2**attempt
                delay = min(delay, MAX_BACKOFF_SECONDS)
                logger.warning("FYERS request failed (%s); retry %d/%d in %.1fs",
                               retry.reason, attempt + 1, self._max_retries, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def redact(self, text: str) -> str:
        """Scrub the credentials from ``text`` (used before saving raw responses)."""
        return self._credentials.redact(text)

    # -- internals ----------------------------------------------------------------------

    def _pace(self) -> None:
        if self._last_request_at is not None:
            wait = self._min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _safe(self, text: str) -> str:
        return self._credentials.redact(text)[:_MAX_MESSAGE_CHARS]

    def _attempt(self, url: str, headers: Mapping[str, str]) -> dict[str, Any]:
        try:
            response = self._transport(url, headers, self._timeout)
        except FyersNetworkError as exc:
            # Built from fixed wording by netdiag, so no length cap (it would cut the remediation hint);
            # credential redaction is kept as a safety net.
            error = FyersNetworkError(self.redact(str(exc)), category=exc.category, request_sent=exc.request_sent)
            if exc.category in PERMANENT_CATEGORIES:  # a bad trust chain does not fix itself: fail now
                raise error from None
            raise _Retry(error, f"network error ({exc.category})") from None

        text = response.body.decode("utf-8", errors="replace")
        payload = _parse_json(text)
        status = response.status
        code = payload.get("code") if isinstance(payload, dict) else None
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        message = self._safe(str(payload.get("message", ""))) if isinstance(payload, dict) else ""
        detail = message or self._safe(text.strip())

        if status == 429:
            retry_after = _retry_after(response.headers)
            raise _Retry(FyersRateLimitError(f"rate limit exceeded (HTTP 429): {detail}", status=status, code=code),
                         "HTTP 429", retry_after)
        if status >= 500:
            raise _Retry(FyersApiError(f"FYERS server error (HTTP {status}): {detail}", status=status, code=code),
                         f"HTTP {status}")
        if status in (401, 403):
            raise FyersAuthError(f"authentication failed (HTTP {status}): {detail}. "
                                 "Regenerate the access token.", status=status, code=code)
        if not 200 <= status < 300:
            raise FyersApiError(f"FYERS rejected the request (HTTP {status}): {detail}", status=status, code=code)
        if not isinstance(payload, dict):
            raise FyersApiError("FYERS returned a response that is not a JSON object", status=status)

        state = payload.get("s")
        if state in ("ok", "no_data"):
            return payload
        return self._raise_for_error_payload(status, code, message)

    def _raise_for_error_payload(self, status: int, code: int | None, message: str) -> dict[str, Any]:
        lowered = message.lower()
        if code in _RATE_LIMIT_CODES or "rate limit" in lowered:
            raise _Retry(FyersRateLimitError(f"rate limit exceeded (code {code}): {message}", status=status, code=code),
                         f"code {code}")
        if code in _AUTH_CODES or any(word in lowered for word in _AUTH_WORDS):
            raise FyersAuthError(f"authentication failed (code {code}): {message}. "
                                 "Regenerate the access token.", status=status, code=code)
        raise FyersApiError(f"FYERS API error (code {code}): {message or 'no message'}", status=status, code=code)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None
            return seconds if seconds >= 0 else None
    return None
