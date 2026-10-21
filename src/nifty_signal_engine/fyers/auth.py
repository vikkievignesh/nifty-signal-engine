"""Local-only FYERS v3 login helper (Individual App flow): login URL, callback, token exchange.

Flow (endpoints and fields per the official ``fyers-apiv3`` SDK 3.2.0 and a summary of
the official FYERS v3 "individual app" authentication docs):

1. Open ``GET https://api-t1.fyers.in/api/v3/generate-authcode`` with ``client_id``,
   ``redirect_uri``, ``response_type=code`` and a random ``state``.
2. After login FYERS redirects the browser to the registered redirect URL. This module
   serves exactly ``http://127.0.0.1:5000/callback`` and reads ``auth_code`` and ``state``
   from the query string.
3. ``POST https://api-t1.fyers.in/api/v3/validate-authcode`` with JSON
   ``{"grant_type": "authorization_code", "appIdHash": SHA256("app_id:app_secret"),
   "code": <auth_code>}`` returns ``access_token``.

UNVERIFIED (the official pages could not be opened when this was written; handled
fail-closed rather than guessed):

* The exact redirect query format. Only a callback with a matching ``state``, a
  well-formed ``auth_code`` and no ``error``/``error_description`` is accepted; any other
  shape is rejected with a generic message.
* Whether FYERS accepts ``http://127.0.0.1:5000/callback`` as the registered redirect
  URL. It must be registered on the app exactly as written here.

Secrets (app secret, ``appIdHash``, auth code, access token) are never printed, logged,
or placed in exception messages; the HTTP server's own request logging (which would
contain the auth code) is disabled. Nothing here places orders or trades.
"""

from __future__ import annotations

import hmac
import http.server
import json
import logging
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .client import _OPENER, HttpResponse
from .credentials import REDACTED, AuthCredentials, SecretStr
from .errors import AuthFlowError, FyersApiError, FyersAuthError, FyersNetworkError

logger = logging.getLogger(__name__)

AUTH_BASE_URL = "https://api-t1.fyers.in/api/v3"
GENERATE_AUTHCODE_PATH = "/generate-authcode"
VALIDATE_AUTHCODE_PATH = "/validate-authcode"
GRANT_TYPE = "authorization_code"
RESPONSE_TYPE = "code"

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 5000
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"

DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_QUERY_CHARS = 8192
_MAX_QUERY_FIELDS = 20
_AUTH_CODE_RE = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}")
_ACCESS_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}")
_ERROR_KEYS = ("error", "error_description", "message", "s", "code")
_MAX_DIAGNOSTIC_CHARS = 80

PostTransport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


# --- state and login URL ---------------------------------------------------------------------

def generate_state() -> str:
    """A cryptographically random, URL-safe OAuth ``state`` (256 bits)."""
    return secrets.token_urlsafe(32)


def build_login_url(app_id: str, state: str, base_url: str = AUTH_BASE_URL) -> str:
    """The FYERS login URL. Contains the app id and one-time state, never the app secret."""
    query = urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": RESPONSE_TYPE,
        "state": state,
    })
    return f"{base_url}{GENERATE_AUTHCODE_PATH}?{query}"


# --- callback validation (pure, unit-testable) -----------------------------------------------

def _diagnostic(value: str) -> str:
    """Printable-ASCII, short, whitespace-collapsed rendering of untrusted callback text."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._:,-]", "", value)
    return re.sub(r"\s+", " ", cleaned).strip()[:_MAX_DIAGNOSTIC_CHARS]


def evaluate_callback(raw_query: str, expected_state: str) -> SecretStr:
    """Validate the callback query string and return the ``auth_code``.

    Raises :class:`AuthFlowError` (with a message that is safe to show) unless the
    query has exactly one well-formed ``auth_code``, a ``state`` equal to
    ``expected_state`` (constant-time comparison) and no ``error``/``error_description``.
    """
    if len(raw_query) > MAX_QUERY_CHARS:
        raise AuthFlowError("callback query is too large")
    try:
        params = urllib.parse.parse_qs(raw_query, keep_blank_values=True, max_num_fields=_MAX_QUERY_FIELDS)
    except ValueError:
        raise AuthFlowError("callback query could not be parsed") from None
    if not params:
        raise AuthFlowError("callback contained no parameters")
    for name in ("state", "auth_code"):
        if len(params.get(name, [])) > 1:
            raise AuthFlowError(f"callback repeats the {name} parameter")

    received_state = params.get("state", [""])[0]
    if not received_state:
        raise AuthFlowError("callback is missing the state parameter, so it cannot be trusted")
    if not hmac.compare_digest(received_state.encode(), expected_state.encode()):
        raise AuthFlowError("callback state does not match this login attempt (possible forged callback)")

    if "error" in params or "error_description" in params or not params.get("auth_code", [""])[0]:
        details = "; ".join(
            f"{key}={_diagnostic(params[key][0])}" for key in _ERROR_KEYS if params.get(key) and params[key][0]
        )
        raise AuthFlowError("FYERS did not return an authorization code" + (f" ({details})" if details else ""))

    auth_code = params["auth_code"][0]
    if not _AUTH_CODE_RE.fullmatch(auth_code):
        raise AuthFlowError("auth_code in the callback has an unexpected format")
    return SecretStr(auth_code)


# --- loopback callback server ----------------------------------------------------------------

_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>FYERS login</title>"
    b"<p>%s</p>"
)


class _HTTPServer(http.server.HTTPServer):
    allow_reuse_address = False  # never share the port with another listener

    expected_state: str = ""
    result: SecretStr | None = None
    failure: AuthFlowError | None = None

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: forbid port sharing
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # close after each response; browsers' idle sockets cannot stall us
    timeout = 5
    server_version = "nifty-login"
    sys_version = ""

    # The default logging writes the request line -- which contains the auth code -- to stderr.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        return

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _reply(self, status: int, message: str) -> None:
        body = _PAGE % message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        server: _HTTPServer = self.server  # type: ignore[assignment]
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._reply(404, "Not found.")  # e.g. /favicon.ico: ignored, flow keeps waiting
            return
        if self.headers.get("Host") != f"{CALLBACK_HOST}:{server.server_address[1]}":
            self._reply(400, "Bad request.")  # DNS-rebinding guard; ignored, flow keeps waiting
            return
        if server.result is not None or server.failure is not None:
            self._reply(400, "This login attempt has already finished.")
            return
        try:
            server.result = evaluate_callback(parts.query, server.expected_state)
        except AuthFlowError as exc:
            server.failure = exc
            self._reply(400, "Authentication failed. Return to the terminal.")
        else:
            self._reply(200, "Authentication complete. You can close this tab.")

    def _method_not_allowed(self) -> None:
        self._reply(405, "Method not allowed.")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed  # noqa: N815


class CallbackServer:
    """One-shot HTTP server bound to ``127.0.0.1:5000`` serving only ``/callback``.

    The host and path are fixed. ``port`` exists only so tests can use an ephemeral
    port; production code never passes it. Binding failure (port busy) raises
    :class:`AuthFlowError`; there is deliberately no fallback port.
    """

    host = CALLBACK_HOST
    path = CALLBACK_PATH

    def __init__(self, *, port: int = CALLBACK_PORT) -> None:
        try:
            self._httpd = _HTTPServer((CALLBACK_HOST, port), _Handler)
        except OSError as exc:
            raise AuthFlowError(
                f"cannot listen on {CALLBACK_HOST}:{port} ({type(exc).__name__}); "
                "is another program using that port?"
            ) from None
        self.port: int = self._httpd.server_address[1]
        if self._httpd.server_address[0] != CALLBACK_HOST:  # defence in depth
            self._httpd.server_close()
            raise AuthFlowError("callback server is not bound to 127.0.0.1")

    def __enter__(self) -> CallbackServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._httpd.server_close()

    def wait_for_auth_code(self, expected_state: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> SecretStr:
        """Serve requests until a valid callback, a failed callback or the timeout."""
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_SECONDS:.0f} seconds")
        httpd = self._httpd
        httpd.expected_state, httpd.result, httpd.failure = expected_state, None, None
        deadline = time.monotonic() + timeout
        while True:
            if httpd.failure is not None:
                raise httpd.failure
            if httpd.result is not None:
                return httpd.result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthFlowError(f"timed out after {timeout:.0f}s waiting for the FYERS callback")
            httpd.timeout = min(0.5, remaining)
            httpd.handle_request()


# --- token exchange --------------------------------------------------------------------------

def urllib_post_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> HttpResponse:
    """One HTTPS POST using the standard library (redirects are never followed)."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return HttpResponse(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FyersNetworkError(f"network error: {type(exc).__name__}") from None


def exchange_auth_code(
    credentials: AuthCredentials,
    auth_code: SecretStr,
    *,
    transport: PostTransport = urllib_post_transport,
    base_url: str = AUTH_BASE_URL,
    timeout: float = 30.0,
) -> SecretStr:
    """Exchange a single-use ``auth_code`` for an access token. Never retried."""
    if not base_url.startswith("https://"):
        raise ValueError("base_url must be https:// so the app secret hash is never sent in clear text")
    code = auth_code.reveal()

    def scrub(text: str) -> str:
        return credentials.redact(text).replace(code, REDACTED)

    body = json.dumps({
        "grant_type": GRANT_TYPE,
        "appIdHash": credentials.app_id_hash,
        "code": code,
    }).encode()
    response = transport(f"{base_url.rstrip('/')}{VALIDATE_AUTHCODE_PATH}",
                         {"Content-Type": "application/json"}, body, timeout)

    text = response.body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    detail = ""
    status_code: int | None = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        status_code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
        detail = _diagnostic(scrub(str(payload.get("message", ""))))

    rejected = not 200 <= response.status < 300 or not isinstance(payload, dict) or payload.get("s", "ok") != "ok"
    if rejected:
        raise FyersAuthError(
            f"token exchange rejected (HTTP {response.status}, code {status_code})"
            + (f": {detail}" if detail else "")
            + ". Check the app id/secret and redirect URL, then run the login again.",
            status=response.status, code=status_code,
        )
    token = payload.get("access_token")
    if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
        raise FyersApiError("token exchange succeeded but returned no usable access_token "
                            "(missing or unexpected format)", status=response.status, code=status_code)
    return SecretStr(token)
