"""Shared fakes for the FYERS login-helper tests. Every value here is an obvious fake."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

from nifty_signal_engine.fyers import AuthCredentials, AuthFlowError, CallbackServer, HttpResponse

APP_ID = "TESTAPP123-100"
APP_SECRET = "S3CRET-VALUE-xyz987"
APP_ID_HASH = "3c84d5670edb085582f271b0e59292b05c09a24c43a98f0818333cb6c98c2fc9"  # literal SHA256("TESTAPP123-100:S3CRET-VALUE-xyz987"), computed once, not by the code under test
AUTH_CODE = "AUTHCODE.abcdef123456.zzz"
ACCESS_TOKEN = "tok_ABCDEFGHIJKLMNOP.qrstuvwxyz0123"
OLD_TOKEN = "old_TOKEN_0123456789abcdef"


def creds() -> AuthCredentials:
    return AuthCredentials(APP_ID, APP_SECRET)


class ScriptedPost:
    """Fake POST transport: returns the scripted items in order (repeating the last)."""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, dict[str, str], dict]] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body)))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def json_resp(payload, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode())


def token_resp(token: str = ACCESS_TOKEN) -> HttpResponse:
    return json_resp({"s": "ok", "code": 200, "message": "", "access_token": token})


def serve_and_request(requests, *, state="STATE-xyz", timeout=5.0):
    """Run a CallbackServer on an ephemeral loopback port; a thread sends ``requests`` in order.

    ``requests`` is a list of ``(method, target, extra_headers)``. Returns
    ``(outcome, responses)`` where ``outcome`` is the SecretStr or the AuthFlowError.
    """
    responses: list[tuple[int, dict, bytes]] = []
    with CallbackServer(port=0) as server:
        def worker():
            for method, target, extra in requests:
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                headers = {"Host": f"127.0.0.1:{server.port}"}
                headers.update(extra or {})
                conn.request(method, target, headers=headers)
                response = conn.getresponse()
                responses.append((response.status, dict(response.getheaders()), response.read()))
                conn.close()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            outcome = server.wait_for_auth_code(state, timeout)
        except AuthFlowError as exc:
            outcome = exc
        thread.join(10)
    return outcome, responses


def callback_target(state: str, auth_code: str = AUTH_CODE, **extra) -> str:
    params = {"auth_code": auth_code, "state": state, **extra}
    return "/callback?" + urllib.parse.urlencode(params)
