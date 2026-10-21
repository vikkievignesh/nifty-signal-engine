"""Command line: ``python -m nifty_signal_engine.fyers.diagnose``.

Checks, one stage at a time, whether *this Python* can reach the FYERS API host:
DNS -> TCP -> TLS (with full certificate verification) -> one plain HTTPS GET of the host
root. It needs no credentials and sends none: no app id, secret, auth code, token, request
body or header. It does not call the token-exchange endpoint. The host is fixed (this is not
a scanner), and output is built from fixed wording only (see :mod:`.netdiag`).

Why it exists: ``curl`` and Python use different certificate stores. curl may reach the server
while Python's ``urllib`` fails certificate verification (typical for python.org builds on
macOS), which surfaces as the unhelpful ``URLError``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import ssl
import sys
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .auth import AUTH_BASE_URL
from .client import urllib_transport
from .errors import FyersNetworkError
from .netdiag import NetworkCategory, NetworkFailure, classify_network_error, hint_for

FYERS_HOST = urllib.parse.urlsplit(AUTH_BASE_URL).hostname or "api-t1.fyers.in"
FYERS_PORT = 443
PROBE_URL = f"https://{FYERS_HOST}/"  # host root: deliberately not the token-exchange endpoint
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Stage:
    name: str
    status: str  # "ok" | "fail" | "skipped" | "info"
    detail: str
    category: str | None = None
    hint: str | None = None


def _yes(value: object) -> str:
    return "yes" if value else "no"


def runtime_facts(
    env: Mapping[str, str],
    verify_paths: Callable[[], ssl.DefaultVerifyPaths] = ssl.get_default_verify_paths,
) -> Stage:
    """Facts about Python's TLS trust setup (booleans and versions only, no paths or values)."""
    paths = verify_paths()
    cafile = paths.cafile
    cert_file_env = env.get("SSL_CERT_FILE")
    facts = [
        f"python {sys.version.split()[0]} on {sys.platform}",
        ssl.OPENSSL_VERSION,
        f"default CA file present: {_yes(cafile and os.path.isfile(cafile))}",
        f"default CA dir present: {_yes(paths.capath and os.path.isdir(paths.capath))}",
        f"SSL_CERT_FILE set: {_yes(cert_file_env)}" + (f" (file exists: {_yes(os.path.isfile(cert_file_env))})"
                                                       if cert_file_env else ""),
        f"SSL_CERT_DIR set: {_yes(env.get('SSL_CERT_DIR'))}",
        f"certifi importable: {_yes(importlib.util.find_spec('certifi'))}",
    ]
    return Stage("python-tls", "info", "; ".join(facts))


def _fail(name: str, exc: BaseException) -> Stage:
    """Turn any exception into a failed stage using fixed wording only (never ``str(exc)`` of a raw error)."""
    if isinstance(exc, FyersNetworkError):  # already classified (and worded safely) by a transport
        try:
            category = NetworkCategory(exc.category)
        except ValueError:
            category = NetworkCategory.OTHER
        return Stage(name, "fail", str(exc).split(". ")[0], category.value, hint_for(category, exc.request_sent))
    failure: NetworkFailure = classify_network_error(exc)
    return Stage(name, "fail", f"{failure.detail} ({' > '.join(failure.chain)})", failure.category.value, failure.hint)


def run_diagnostics(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    resolver: Callable[..., list] = socket.getaddrinfo,
    connector: Callable[..., socket.socket] = socket.create_connection,
    context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    http_get: Callable[[str, Mapping[str, str], float], object] = urllib_transport,
    verify_paths: Callable[[], ssl.DefaultVerifyPaths] = ssl.get_default_verify_paths,
) -> list[Stage]:
    """Run the staged probe; later stages are skipped once one fails. No credentials are used."""
    stages = [runtime_facts(os.environ if env is None else env, verify_paths)]

    def skip_rest(names: Sequence[str], because: str) -> None:
        stages.extend(Stage(n, "skipped", f"not run: {because} failed") for n in names)

    try:
        addresses = resolver(FYERS_HOST, FYERS_PORT, type=socket.SOCK_STREAM)
        families = sorted({"IPv6" if a[0] == socket.AF_INET6 else "IPv4" for a in addresses})
        stages.append(Stage("dns", "ok", f"resolved {len(addresses)} address(es) ({', '.join(families)})"))
    except Exception as exc:  # noqa: BLE001 - classified, never printed raw
        stages.append(_fail("dns", exc))
        skip_rest(("tcp", "tls", "http"), "dns")
        return stages

    try:
        connector((FYERS_HOST, FYERS_PORT), timeout).close()
        stages.append(Stage("tcp", "ok", f"connected to port {FYERS_PORT}"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("tcp", exc))
        skip_rest(("tls", "http"), "tcp")
        return stages

    try:
        with connector((FYERS_HOST, FYERS_PORT), timeout) as raw:
            with context_factory().wrap_socket(raw, server_hostname=FYERS_HOST) as tls:
                stages.append(Stage("tls", "ok", f"handshake and certificate verification succeeded ({tls.version()})"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("tls", exc))
        skip_rest(("http",), "tls")
        return stages

    try:
        response = http_get(PROBE_URL, {}, timeout)  # no headers: nothing credential-bearing can be sent
        stages.append(Stage("http", "ok", f"server answered HTTP {response.status} (this probe sends no "
                                          "credentials; any HTTP status means Python's HTTPS stack works)"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("http", exc))
    return stages


def format_report(stages: Sequence[Stage]) -> str:
    lines = [f"FYERS connectivity check for {FYERS_HOST}:{FYERS_PORT} (no credentials are used or sent)"]
    for stage in stages:
        lines.append(f"  {stage.name:<10} {stage.status.upper():<8} {stage.detail}")
    failed = next((s for s in stages if s.status == "fail"), None)
    if failed:
        lines.append(f"\nFirst failure: {failed.name} ({failed.category}).")
        lines.append(failed.hint or "")
    elif all(s.status in ("ok", "info") for s in stages):
        lines.append("\nAll stages passed: DNS, TCP, TLS and HTTPS work from this Python. If the token exchange "
                     "still fails, the login error now shows the HTTP status or category; report that.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, out: Callable[[str], None] = print, **dependencies) -> int:
    parser = argparse.ArgumentParser(prog="python -m nifty_signal_engine.fyers.diagnose", description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="seconds per network step (default 10)")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        print("error: --timeout must be between 0 and 120 seconds", file=sys.stderr)
        return 1
    stages = run_diagnostics(timeout=args.timeout, **dependencies)
    out(format_report(stages))
    return 1 if any(s.status == "fail" for s in stages) else 0


if __name__ == "__main__":
    sys.exit(main())
