"""Command line: ``python -m nifty_signal_engine.fyers.login``.

Interactive, local-only FYERS v3 Individual App login. It opens the FYERS login page,
receives the redirect on ``http://127.0.0.1:5000/callback``, verifies the OAuth state,
exchanges the auth code for an access token and stores only ``FYERS_ACCESS_TOKEN`` in
the local ``.env``. Needs ``FYERS_APP_ID`` and ``FYERS_APP_SECRET`` (environment or
``.env``). It never prints secrets, never places orders and runs no trading logic.
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import envfile
from .auth import (
    CALLBACK_PATH,
    CALLBACK_PORT,
    CALLBACK_PORT_VAR,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CALLBACK_PORT,
    MAX_TIMEOUT_SECONDS,
    MIN_CALLBACK_PORT,
    CallbackServer,
    PostTransport,
    build_login_url,
    describe_exchange_request,
    exchange_auth_code,
    generate_state,
    urllib_post_transport,
    validate_callback_port,
)
from .credentials import ACCESS_TOKEN_VAR, AuthCredentials, load_auth_credentials, lookup_setting
from .errors import AuthFlowError, FyersError

logger = logging.getLogger("nifty_signal_engine.fyers")


def run_login(
    credentials: AuthCredentials,
    *,
    env_file: str | Path = ".env",
    port: int = CALLBACK_PORT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    open_url: Callable[[str], bool] = webbrowser.open,
    no_browser: bool = False,
    print_url: bool = False,
    post_transport: PostTransport = urllib_post_transport,
    server_factory: Callable[[int], CallbackServer] = lambda port: CallbackServer(port=port),
    say: Callable[[str], None] = print,
) -> None:
    """Run one login attempt; on success ``FYERS_ACCESS_TOKEN`` is updated in ``env_file``."""
    port = validate_callback_port(port)
    envfile.preflight(env_file)  # fail before the user logs in, not after
    state = generate_state()
    with server_factory(port) as server:  # exactly one bind attempt; no fallback port
        redirect_uri = server.redirect_uri  # from the port actually bound, so it cannot drift
        url = build_login_url(credentials.app_id, state, redirect_uri=redirect_uri)
        say(f"Listening on {redirect_uri}. That exact URL must be saved as the redirect URL of your "
            "FYERS app.")
        if port != CALLBACK_PORT:
            say(f"NOTE: custom port {port}. Before continuing, update the redirect URL in the FYERS "
                f"dashboard to exactly {redirect_uri} and save it; otherwise FYERS will reject the login.")
        if print_url:
            say("Open this login URL in your browser (it contains your app id and a one-time state):")
            say(url)
        if not no_browser:
            try:
                opened = bool(open_url(url))
            except Exception:  # noqa: BLE001 - browser launchers raise assorted errors
                opened = False
            if opened:
                say("Opened the FYERS login page in your browser (URL not shown; use --print-url to see it).")
            elif not print_url:
                raise AuthFlowError("could not open a browser; re-run with --print-url and open the URL yourself")
        elif not print_url:
            raise AuthFlowError("--no-browser needs --print-url, otherwise there is no way to start the login")
        say(f"Waiting up to {timeout:.0f}s for the callback on {CALLBACK_PATH} ...")
        auth_code = server.wait_for_auth_code(state, timeout)
    token = exchange_auth_code(credentials, auth_code, transport=post_transport)
    envfile.set_env_value(env_file, ACCESS_TOKEN_VAR, token.reveal())
    say(f"{ACCESS_TOKEN_VAR} updated in {env_file} (value not shown, file mode 0600). "
        "Access tokens expire; run this command again when the downloader reports an authentication failure.")


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    open_url: Callable[[str], bool] = webbrowser.open,
    post_transport: PostTransport = urllib_post_transport,
    server_factory: Callable[[int], CallbackServer] = lambda port: CallbackServer(port=port),
) -> int:
    parser = argparse.ArgumentParser(prog="python -m nifty_signal_engine.fyers.login", description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"),
                        help="local, git-ignored file holding FYERS_APP_ID/FYERS_APP_SECRET; "
                             "FYERS_ACCESS_TOKEN is written here (default .env)")
    parser.add_argument("--port", type=str, default=None,  # validated by validate_callback_port, like the env var
                        help=f"callback port on 127.0.0.1 ({MIN_CALLBACK_PORT}-{MAX_CALLBACK_PORT}); default "
                             f"{CALLBACK_PORT} or ${CALLBACK_PORT_VAR}. It must match the redirect URI saved in "
                             "the FYERS dashboard (http://127.0.0.1:<port>/callback)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"seconds to wait for the browser callback (max {MAX_TIMEOUT_SECONDS:.0f})")
    parser.add_argument("--print-url", action="store_true",
                        help="print the login URL (contains the app id and one-time state; never the secret)")
    parser.add_argument("--no-browser", action="store_true", help="do not try to open a browser")
    parser.add_argument("--check", action="store_true",
                        help="offline: describe the token-exchange request and sanity-check the App ID format "
                             "and redirect URI; no server, no network, no secret printed")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        envfile.preflight(args.env_file)
        credentials = load_auth_credentials(env, args.env_file)
        configured = args.port if args.port is not None else lookup_setting(CALLBACK_PORT_VAR, env, args.env_file)
        port = validate_callback_port(configured) if configured is not None else CALLBACK_PORT
        if args.check:
            print("\n".join(describe_exchange_request(credentials, port)))
            return 0
        run_login(credentials, env_file=args.env_file, port=port, timeout=args.timeout, open_url=open_url,
                  no_browser=args.no_browser, print_url=args.print_url,
                  post_transport=post_transport, server_factory=server_factory)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except (FyersError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # messages are credential-free by construction
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
