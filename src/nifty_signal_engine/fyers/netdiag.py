"""Safe classification of low-level network failures.

``urllib`` wraps most connection problems in ``URLError`` whose *type name* is always
``URLError``; the real cause (DNS, refused connection, TLS certificate, ...) lives in
``exc.reason``. This module unwraps it and maps it to a small, fixed vocabulary.

Nothing here ever uses ``str(exc)``: output is assembled only from fixed phrases, exception
*class names*, ``errno`` symbolic names and a table of known OpenSSL verify codes. A request
body, URL, header, auth code, secret or token therefore cannot leak through an error
message even if a library put one into an exception.
"""

from __future__ import annotations

import errno
import http.client
import socket
import ssl
import urllib.error
from dataclasses import dataclass
from enum import StrEnum

from .errors import FyersNetworkError


class NetworkCategory(StrEnum):
    DNS = "dns"
    CONNECTION = "connection"
    TLS_CERTIFICATE = "tls_certificate"
    TLS = "tls"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    OTHER = "other"


# Failures that retrying cannot fix: a bad trust chain or handshake is deterministic.
PERMANENT_CATEGORIES = frozenset({NetworkCategory.TLS_CERTIFICATE, NetworkCategory.TLS})

# OpenSSL X509 verify codes -> fixed descriptions (the library's own text is never shown).
VERIFY_MESSAGES = {
    2: "unable to get issuer certificate",
    9: "certificate is not yet valid",
    10: "certificate has expired",
    18: "self-signed certificate",
    19: "self-signed certificate in certificate chain",
    20: "unable to get local issuer certificate",
    21: "unable to verify the first certificate",
    62: "certificate does not match the host name",
}

_NOT_SENT = {errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EADDRNOTAVAIL}
_RESET = {errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED}

_DETAIL = {
    NetworkCategory.DNS: "the host name could not be resolved",
    NetworkCategory.CONNECTION: "the connection could not be established or was dropped",
    NetworkCategory.TLS: "the TLS handshake failed",
    NetworkCategory.TIMEOUT: "the request timed out",
    NetworkCategory.PROTOCOL: "the server sent a malformed or truncated HTTP response",
    NetworkCategory.OTHER: "an unexpected network failure occurred",
}

_HINT_CERT = (
    "Nothing was sent, so the auth code was not used. Common causes: Python has no usable CA bundle "
    "(python.org builds on macOS: run the 'Install Certificates.command' that ships with Python, or "
    "`pip install certifi` and `export SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())')`), "
    "or a proxy/antivirus re-signs HTTPS traffic with a CA Python does not trust. Never disable certificate "
    "verification. Run `python -m nifty_signal_engine.fyers.diagnose`, fix the cause, then run the login again"
)
_HINTS = {
    (NetworkCategory.TLS_CERTIFICATE, "no"): _HINT_CERT,
    (NetworkCategory.TLS, "no"): (
        "It failed before any data was sent, so the auth code was not used. Possible causes: an unsupported "
        "protocol or cipher, a middlebox, or a proxy. Run `python -m nifty_signal_engine.fyers.diagnose`, "
        "then run the login again"
    ),
    (NetworkCategory.DNS, "no"): (
        "Nothing was sent, so the auth code was not used. Check your network, DNS and VPN settings, then run "
        "the login again"
    ),
    (NetworkCategory.CONNECTION, "no"): (
        "Nothing was sent, so the auth code was not used. Check firewall, VPN and proxy settings, then run the "
        "login again"
    ),
    (NetworkCategory.CONNECTION, "maybe"): (
        "FYERS may have received the request, so the auth code may already be used up. Run the login again"
    ),
    (NetworkCategory.TIMEOUT, "unknown"): (
        "It is unknown whether FYERS received the request, so the auth code may already be used up. Run the "
        "login again"
    ),
    (NetworkCategory.PROTOCOL, "maybe"): (
        "The auth code may already be used up. Run the login again"
    ),
}
_DEFAULT_HINT = (
    "Run `python -m nifty_signal_engine.fyers.diagnose` to check DNS, TCP, TLS and HTTP separately, then "
    "run the login again"
)


@dataclass(frozen=True)
class NetworkFailure:
    category: NetworkCategory
    request_sent: str  # "no" | "maybe" | "unknown"
    chain: tuple[str, ...]  # exception class names only
    detail: str  # fixed wording, safe to display
    hint: str


def _unwrap(exc: BaseException) -> tuple[BaseException, tuple[str, ...]]:
    chain = [type(exc).__name__]
    for _ in range(3):
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
            chain.append(type(exc).__name__)
        else:
            break
    return exc, tuple(chain)


def hint_for(category: NetworkCategory, request_sent: str) -> str:
    return _HINTS.get((category, request_sent), _DEFAULT_HINT)


def classify_network_error(exc: BaseException) -> NetworkFailure:
    """Map an exception raised by an HTTP/socket/TLS call to a safe :class:`NetworkFailure`."""
    inner, chain = _unwrap(exc)
    category, request_sent, detail = NetworkCategory.OTHER, "unknown", ""

    if isinstance(inner, ssl.SSLCertVerificationError):
        category, request_sent = NetworkCategory.TLS_CERTIFICATE, "no"
        reason = VERIFY_MESSAGES.get(getattr(inner, "verify_code", None), "reason not recognised")
        detail = f"TLS certificate verification failed ({reason})"
    elif isinstance(inner, ssl.SSLError):
        category = NetworkCategory.TLS
        request_sent = "unknown" if isinstance(inner, (ssl.SSLEOFError, ssl.SSLZeroReturnError)) else "no"
    elif isinstance(inner, (socket.gaierror, socket.herror)):
        category, request_sent = NetworkCategory.DNS, "no"
    elif isinstance(inner, TimeoutError):
        category = NetworkCategory.TIMEOUT
    elif isinstance(inner, ConnectionRefusedError):
        category, request_sent = NetworkCategory.CONNECTION, "no"
        detail = "the connection was refused (ECONNREFUSED)"
    elif isinstance(inner, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        category, request_sent = NetworkCategory.CONNECTION, "maybe"
        detail = f"the connection was dropped ({errno.errorcode.get(getattr(inner, 'errno', None), 'reset')})"
    elif isinstance(inner, OSError):
        code = inner.errno
        if code in _NOT_SENT:
            category, request_sent = NetworkCategory.CONNECTION, "no"
            detail = f"the network or host is unreachable ({errno.errorcode.get(code, 'unreachable')})"
        elif code == errno.ETIMEDOUT:
            category = NetworkCategory.TIMEOUT
        elif code in _RESET:
            category, request_sent = NetworkCategory.CONNECTION, "maybe"
            detail = f"the connection was dropped ({errno.errorcode.get(code, 'reset')})"
    elif isinstance(inner, http.client.HTTPException):
        category, request_sent = NetworkCategory.PROTOCOL, "maybe"

    return NetworkFailure(
        category=category,
        request_sent=request_sent,
        chain=chain,
        detail=detail or _DETAIL.get(category, _DETAIL[NetworkCategory.OTHER]),
        hint=hint_for(category, request_sent),
    )


def network_error_from(exc: BaseException) -> FyersNetworkError:
    """Build a :class:`FyersNetworkError` whose text is safe to print and log."""
    failure = classify_network_error(exc)
    message = (f"network error [{failure.category.value}]: {failure.detail} "
               f"({' > '.join(failure.chain)}). {failure.hint}")
    return FyersNetworkError(message, category=failure.category.value, request_sent=failure.request_sent)
