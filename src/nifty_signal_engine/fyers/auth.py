"""Local-only FYERS v3 login helper (Individual App flow): login URL, callback, token exchange.

Flow (endpoints and fields per the official ``fyers-apiv3`` SDK 3.2.0 and a summary of
the official FYERS v3 "individual app" authentication docs):

1. Open ``GET https://api-t1.fyers.in/api/v3/generate-authcode`` with ``client_id``,
   ``redirect_uri``, ``response_type=code`` and a random ``state``. ``redirect_uri`` is
   always the URI of the server that is really listening and must equal the redirect URI
   saved on the FYERS app.
2. After login FYERS redirects the browser to the registered redirect URL. This module
   serves ``http://127.0.0.1:<port>/callback`` (default port 5000) and reads ``auth_code`` and ``state``
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
import http.client
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
from dataclasses import dataclass
from typing import Any

from .client import _OPENER, HttpResponse, validate_user_agent
from .credentials import REDACTED, AuthCredentials, SecretStr
from .errors import AuthFlowError, FyersApiError, FyersAuthError
from .netdiag import network_error_from

logger = logging.getLogger(__name__)

AUTH_BASE_URL = "https://api-t1.fyers.in/api/v3"
GENERATE_AUTHCODE_PATH = "/generate-authcode"
VALIDATE_AUTHCODE_PATH = "/validate-authcode"
GRANT_TYPE = "authorization_code"
RESPONSE_TYPE = "code"

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 5000  # default; must match the redirect URI registered on the FYERS app
CALLBACK_PATH = "/callback"
MIN_CALLBACK_PORT, MAX_CALLBACK_PORT = 1024, 65535
CALLBACK_PORT_VAR = "FYERS_CALLBACK_PORT"


def redirect_uri_for(port: int) -> str:
    """The exact redirect URI for ``port``: ``http://127.0.0.1:<port>/callback``."""
    return f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"


REDIRECT_URI = redirect_uri_for(CALLBACK_PORT)  # the default registered redirect URI


def validate_callback_port(value: object) -> int:
    """Return ``value`` as a usable explicit callback port (1024-65535) or raise.

    Port 0 (OS-assigned) is refused: an unpredictable port could never match the
    redirect URI registered in the FYERS dashboard.
    """
    if isinstance(value, bool):
        raise AuthFlowError("callback port must be an integer")
    try:
        port = int(str(value).strip())
    except ValueError:
        raise AuthFlowError("callback port must be an integer") from None
    if not MIN_CALLBACK_PORT <= port <= MAX_CALLBACK_PORT:
        raise AuthFlowError(f"callback port must be between {MIN_CALLBACK_PORT} and {MAX_CALLBACK_PORT}")
    return port

DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_QUERY_CHARS = 8192
_MAX_QUERY_FIELDS = 20
_AUTH_CODE_RE = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}")
_ACCESS_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}")
_ERROR_KEYS = ("error", "error_description", "message", "s", "code")
_MAX_DIAGNOSTIC_CHARS = 80
_MAX_API_MESSAGE_CHARS = 120
_API_MESSAGE_KEYS = ("message", "error_description", "error", "detail")  # the only body fields ever read
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9._~+/=-]{24,}")  # hashes, JWTs, long codes: never shown
_APP_ID_SHAPE = re.compile(r"[A-Za-z0-9]+-[0-9]+")  # FYERS dashboard format, e.g. "ABCDE12345-100"

PostTransport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


# --- state and login URL ---------------------------------------------------------------------

def generate_state() -> str:
    """A cryptographically random, URL-safe OAuth ``state`` (256 bits)."""
    return secrets.token_urlsafe(32)


def build_login_url(app_id: str, state: str, base_url: str = AUTH_BASE_URL,
                    redirect_uri: str = REDIRECT_URI) -> str:
    """The FYERS login URL. Contains the app id and one-time state, never the app secret.

    ``redirect_uri`` must be the URI of the server that is actually listening
    (``CallbackServer.redirect_uri``) and the one registered on the FYERS app.
    """
    query = urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": RESPONSE_TYPE,
        "state": state,
    })
    return f"{base_url}{GENERATE_AUTHCODE_PATH}?{query}"


# --- callback validation (pure, unit-testable) -----------------------------------------------

def _diagnostic(value: str, limit: int = _MAX_DIAGNOSTIC_CHARS) -> str:
    """Printable-ASCII, short, whitespace-collapsed rendering of untrusted text."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._:,*-]", "", value)
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]


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
    """One-shot HTTP server bound to ``127.0.0.1:<port>`` serving only ``/callback``.

    The host and path are fixed. ``port`` defaults to 5000 (the registered redirect URI)
    and can be set explicitly; production callers validate it with
    :func:`validate_callback_port`, tests may pass ``0`` for an ephemeral port. There is
    exactly one bind attempt: a busy port raises :class:`AuthFlowError` and there is
    deliberately no fallback port.
    """

    host = CALLBACK_HOST
    path = CALLBACK_PATH

    def __init__(self, *, port: int = CALLBACK_PORT) -> None:
        try:
            self._httpd = _HTTPServer((CALLBACK_HOST, port), _Handler)
        except OSError as exc:
            raise AuthFlowError(
                f"cannot listen on {CALLBACK_HOST}:{port} ({type(exc).__name__}); another program is "
                f"probably using that port. Free it, or choose another with --port / {CALLBACK_PORT_VAR} "
                "after updating AND saving the matching redirect URI in the FYERS dashboard"
            ) from None
        self.port: int = self._httpd.server_address[1]
        if self._httpd.server_address[0] != CALLBACK_HOST:  # defence in depth
            self._httpd.server_close()
            raise AuthFlowError("callback server is not bound to 127.0.0.1")

    @property
    def redirect_uri(self) -> str:
        """Redirect URI for the port this server is really bound to."""
        return redirect_uri_for(self.port)

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
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise network_error_from(exc) from None  # classified, fixed wording; never str(exc)


@dataclass(frozen=True)
class ResponseSummary:
    """Safe description of an error response: shape plus an allowlisted ``code``/``message``.

    The raw body, other JSON fields, headers and cookies are never exposed.
    """

    shape: str  # json-object | json-other | html | text | empty | other
    media_type: str  # application/json | text/html | text/plain | other | none
    size: int  # body size in bytes (a number, not content)
    code: int | None
    message: str  # scrubbed, sanitised, truncated; may be empty

    def describe(self) -> str:
        if self.shape == "json-object" and (self.code is not None or self.message):
            parts = []
            if self.code is not None:
                parts.append(f"API code {self.code}")
            if self.message:
                parts.append(f'message "{self.message}"')
            what = ", ".join(parts)
        else:
            what = {
                "json-object": "a JSON object with no API code or message",
                "json-other": "JSON that is not an object",
                "html": "an HTML page, not a JSON API error",
                "text": "a plain-text body (content not shown)",
                "empty": "an empty body",
            }.get(self.shape, "a body in an unrecognised format (content not shown)")
        return f"{what} [{self.media_type}, {self.size} bytes]"


def _media_type(headers: Mapping[str, str]) -> str:
    raw = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    base = raw.split(";", 1)[0].strip().lower()
    if base in ("application/json", "text/html", "text/plain"):
        return base
    return "other" if base else "none"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,9}", value.strip()):
        return int(value)
    return None


def summarise_response(response: HttpResponse, scrub: Callable[[str], str]) -> ResponseSummary:
    """Describe ``response`` without exposing its body. Only ``code`` and a message key are read."""
    raw = response.body
    media_type = _media_type(response.headers)
    if not raw.strip():
        return ResponseSummary("empty", media_type, len(raw), None, "")
    try:
        text = raw.decode("utf-8")  # strict: undecodable bytes must not be mistaken for text
    except UnicodeDecodeError:
        return ResponseSummary("other", media_type, len(raw), None, "")
    try:
        payload = json.loads(text)
    except ValueError:
        sample = text[:2000]
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample) / len(sample)
        shape = ("html" if media_type == "text/html" or text.lstrip()[:1] == "<"
                 else "text" if printable > 0.9 else "other")
        return ResponseSummary(shape, media_type, len(raw), None, "")
    if not isinstance(payload, dict):
        return ResponseSummary("json-other", media_type, len(raw), None, "")
    message = ""
    for key in _API_MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            message = _diagnostic(_OPAQUE_RUN.sub(REDACTED, scrub(value)), _MAX_API_MESSAGE_CHARS)
            break
    return ResponseSummary("json-object", media_type, len(raw), _as_int(payload.get("code")), message)


def describe_exchange_request(credentials: AuthCredentials, port: int, user_agent: str | None = None) -> list[str]:
    """Offline description of the token-exchange request. Sends nothing; prints no secret or hash."""
    app_id, secret = credentials.app_id, credentials.app_secret
    odd = re.search(r"[^A-Za-z0-9_-]", app_id) is not None
    quoted = lambda v: v[:1] in "\"'`" or v[-1:] in "\"'`"  # noqa: E731
    return [
        "Token exchange request (offline check; nothing is sent):",
        f"  request     : POST {AUTH_BASE_URL}{VALIDATE_AUTHCODE_PATH}",
        "  header      : Content-Type: application/json",
        "  User-Agent  : " + (f"{user_agent}   (EXPERIMENTAL override, set with --user-agent)" if user_agent
                              else "the urllib default, Python-urllib/<python version> (no override)"),
        f'  JSON fields : grant_type="{GRANT_TYPE}", appIdHash=<64 hex chars>, code=<auth code from the callback>',
        '  appIdHash   : SHA-256 over "<App ID>:<App Secret>" -- the full App ID exactly as the FYERS dashboard shows it',
        "App ID checks (the value is never printed):",
        f"  matches the dashboard format <ID>-<digits> : {'yes' if _APP_ID_SHAPE.fullmatch(app_id) else 'NO'}"
        + ("" if _APP_ID_SHAPE.fullmatch(app_id) else "   <- a missing '-100'-style suffix changes the hash"),
        f"  has characters other than letters/digits/-/_: {'YES' if odd else 'no'}",
        f"  wrapped in quote characters                 : {'YES' if quoted(app_id) else 'no'}",
        "App Secret checks (the value is never printed):",
        f"  wrapped in quote characters                 : {'YES' if quoted(secret) else 'no'}",
        f"Redirect URI the login URL will carry         : {redirect_uri_for(port)}",
        "  (must equal the redirect URL saved on the FYERS app, character for character)",
    ]


def exchange_auth_code(
    credentials: AuthCredentials,
    auth_code: SecretStr,
    *,
    transport: PostTransport = urllib_post_transport,
    base_url: str = AUTH_BASE_URL,
    timeout: float = 30.0,
    user_agent: str | None = None,
) -> SecretStr:
    """Exchange a single-use ``auth_code`` for an access token. Never retried.

    ``user_agent`` is an EXPERIMENTAL, opt-in diagnostic. By default (``None``) no ``User-Agent``
    header is set here and urllib adds its own ``Python-urllib/<version>``; the request body,
    URL and other headers are identical either way.
    """
    if not base_url.startswith("https://"):
        raise ValueError("base_url must be https:// so the app secret hash is never sent in clear text")
    code = auth_code.reveal()

    def scrub(text: str) -> str:
        return credentials.redact(text, code)

    body = json.dumps({
        "grant_type": GRANT_TYPE,
        "appIdHash": credentials.app_id_hash,
        "code": code,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if user_agent is not None:
        headers["User-Agent"] = validate_user_agent(user_agent)
    response = transport(f"{base_url.rstrip('/')}{VALIDATE_AUTHCODE_PATH}", headers, body, timeout)

    summary = summarise_response(response, scrub)
    try:
        payload = json.loads(response.body.decode("utf-8", errors="replace"))
    except ValueError:
        payload = None

    rejected = not 200 <= response.status < 300 or not isinstance(payload, dict) or payload.get("s", "ok") != "ok"
    if rejected:
        shape_note = "" if _APP_ID_SHAPE.fullmatch(credentials.app_id) else (
            " Your App ID does not look like the dashboard format <ID>-<digits>; use it exactly as the "
            "dashboard shows it, including the suffix.")
        raise FyersAuthError(
            f"token exchange rejected (HTTP {response.status}): {summary.describe()}."
            + shape_note
            + " Check FYERS_APP_ID / FYERS_APP_SECRET and the saved redirect URL, then run the login again "
              "for a fresh auth code.",
            status=response.status, code=summary.code,
        )
    token = payload.get("access_token")
    if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
        raise FyersApiError("token exchange succeeded but returned no usable access_token "
                            "(missing or unexpected format)", status=response.status, code=summary.code)
    return SecretStr(token)
