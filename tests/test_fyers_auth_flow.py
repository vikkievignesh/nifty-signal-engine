import http.server
import json
import threading
import urllib.parse

import pytest

import nifty_signal_engine.fyers.auth as auth
from nifty_signal_engine.fyers import (
    AuthCredentials,
    AuthFlowError,
    CallbackServer,
    CredentialsError,
    FyersApiError,
    FyersAuthError,
    FyersNetworkError,
    HttpResponse,
    MissingCredentialsError,
    SecretStr,
    exchange_auth_code,
    generate_state,
    load_auth_credentials,
    urllib_transport,
)
from nifty_signal_engine.fyers.auth import (
    CALLBACK_HOST,
    CALLBACK_PATH,
    CALLBACK_PORT,
    REDIRECT_URI,
    build_login_url,
    evaluate_callback,
    redirect_uri_for,
    urllib_post_transport,
    validate_callback_port,
)

from fyers_auth_helpers import (
    ACCESS_TOKEN,
    APP_ID,
    APP_ID_HASH,
    APP_SECRET,
    AUTH_CODE,
    ScriptedPost,
    callback_target,
    creds,
    json_resp,
    serve_and_request,
    token_resp,
)

STATE = "STATE-xyz"


# --- credentials -------------------------------------------------------------------------------

def test_app_id_hash_matches_precomputed_vector():
    assert creds().app_id_hash == APP_ID_HASH


def test_auth_credentials_are_masked_and_redactable():
    c = creds()
    for text in (repr(c), str(c), f"{c}", repr([c])):
        assert APP_SECRET not in text and APP_ID not in text
    cleaned = c.redact(f"{APP_SECRET} {APP_ID_HASH} {APP_ID}")
    assert APP_SECRET not in cleaned and APP_ID_HASH not in cleaned and APP_ID not in cleaned


@pytest.mark.parametrize("app_id, secret", [("", "s"), ("a", ""), ("a b", "s"), ("a", "s\n")])
def test_auth_credentials_reject_bad_values(app_id, secret):
    with pytest.raises(CredentialsError):
        AuthCredentials(app_id, secret)


def test_load_auth_credentials_from_env_and_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"FYERS_APP_ID={APP_ID}\nFYERS_APP_SECRET={APP_SECRET}\n")
    dotenv.chmod(0o600)
    assert load_auth_credentials({}, dotenv).app_secret == APP_SECRET
    assert load_auth_credentials({"FYERS_APP_SECRET": "from-env"}, dotenv).app_secret == "from-env"


def test_load_auth_credentials_missing_names_variables_not_values(tmp_path):
    with pytest.raises(MissingCredentialsError) as info:
        load_auth_credentials({"FYERS_APP_ID": APP_ID}, tmp_path / "none.env")
    assert "FYERS_APP_SECRET" in str(info.value) and APP_ID not in str(info.value)


def test_secretstr_never_reveals_by_accident():
    secret = SecretStr(ACCESS_TOKEN)
    for text in (repr(secret), str(secret), f"{secret}", repr([secret]), "%s" % secret):
        assert ACCESS_TOKEN not in text
    assert secret.reveal() == ACCESS_TOKEN
    with pytest.raises(TypeError):
        __import__("pickle").dumps(secret)


# --- state and login URL -----------------------------------------------------------------------

def test_state_is_random_url_safe_and_long():
    states = {generate_state() for _ in range(200)}
    assert len(states) == 200
    assert all(len(s) >= 43 and set(s) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
               for s in states)


def test_state_comes_from_the_secrets_module(monkeypatch):
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda n: f"called-with-{n}")
    assert generate_state() == "called-with-32"  # 32 bytes = 256 bits


def test_login_url_has_documented_parameters_and_no_secret():
    url = build_login_url(APP_ID, STATE)
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api-t1.fyers.in/api/v3/generate-authcode"
    assert dict(urllib.parse.parse_qsl(parts.query)) == {
        "client_id": APP_ID, "redirect_uri": "http://127.0.0.1:5000/callback",
        "response_type": "code", "state": STATE,
    }
    assert APP_SECRET not in url and APP_ID_HASH not in url


def test_fixed_callback_constants():
    assert (CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH) == ("127.0.0.1", 5000, "/callback")
    assert REDIRECT_URI == "http://127.0.0.1:5000/callback"


# --- callback validation (pure) ----------------------------------------------------------------

def q(**params):
    return urllib.parse.urlencode(params)


def test_valid_callback_returns_masked_auth_code():
    code = evaluate_callback(q(auth_code=AUTH_CODE, state=STATE), STATE)
    assert code.reveal() == AUTH_CODE and AUTH_CODE not in repr(code)


def test_extra_success_parameters_are_ignored():
    assert evaluate_callback(q(s="ok", code="200", auth_code=AUTH_CODE, state=STATE), STATE).reveal() == AUTH_CODE


@pytest.mark.parametrize("query, expect", [
    ("", "no parameters"),
    (q(auth_code=AUTH_CODE), "missing the state"),
    (q(auth_code=AUTH_CODE, state=""), "missing the state"),
    (q(auth_code=AUTH_CODE, state="WRONG"), "does not match"),
    (q(auth_code=AUTH_CODE, state=STATE + "x"), "does not match"),
    ("state=A&state=B&auth_code=" + AUTH_CODE, "repeats the state"),
    (f"state={STATE}&auth_code=aaaaaaaa1&auth_code=aaaaaaaa2", "repeats the auth_code"),
    (q(state=STATE), "did not return an authorization code"),
    (q(state=STATE, auth_code=""), "did not return an authorization code"),
    (q(state=STATE, auth_code="short"), "unexpected format"),
    (q(state=STATE, auth_code="has space in it!"), "unexpected format"),
    (q(state=STATE, auth_code="x" * 5000), "unexpected format"),
])
def test_invalid_callbacks_are_rejected_with_safe_messages(query, expect):
    with pytest.raises(AuthFlowError, match=expect) as info:
        evaluate_callback(query, STATE)
    assert STATE not in str(info.value) and AUTH_CODE not in str(info.value)


def test_error_callback_is_rejected_even_with_an_auth_code_present():
    with pytest.raises(AuthFlowError, match="access_denied"):
        evaluate_callback(q(error="access_denied", auth_code=AUTH_CODE, state=STATE), STATE)


def test_error_diagnostics_are_sanitised_and_bounded():
    nasty = "bad\x1b[31m<script>alert(1)</script>\n" + "A" * 500
    with pytest.raises(AuthFlowError) as info:
        evaluate_callback(q(error_description=nasty, state=STATE), STATE)
    message = str(info.value)
    assert "\x1b" not in message and "<" not in message and "\n" not in message
    assert len(message) < 250


def test_untrusted_error_callback_without_valid_state_reveals_nothing():
    with pytest.raises(AuthFlowError) as info:
        evaluate_callback(q(error="x_attacker_text", state="WRONG"), STATE)
    assert "attacker" not in str(info.value)


def test_oversized_and_overlong_queries_are_rejected():
    with pytest.raises(AuthFlowError, match="too large"):
        evaluate_callback("a=" + "b" * 9000, STATE)
    with pytest.raises(AuthFlowError, match="could not be parsed"):
        evaluate_callback("&".join(f"k{i}=v" for i in range(40)), STATE)


# --- loopback server ---------------------------------------------------------------------------

class _RecordingServer:
    """Stands in for the HTTP server so the production bind address can be checked without
    opening a socket (port 5000 is often taken, e.g. by macOS Control Center / AirPlay Receiver)."""

    def __init__(self, bound, host_reported=None):
        self.bound = bound
        self.host_reported = host_reported
        self.closed = False

    def __call__(self, address, handler):
        self.bound.append((address, handler))
        self.server_address = (self.host_reported or address[0], address[1])
        return self

    def server_close(self):
        self.closed = True


def test_default_server_binds_only_127_0_0_1_port_5000(monkeypatch):
    bound = []
    recorder = _RecordingServer(bound)
    monkeypatch.setattr(auth, "_HTTPServer", recorder)
    with CallbackServer() as server:  # production defaults: no arguments
        assert server.port == 5000
    assert [address for address, _ in bound] == [("127.0.0.1", 5000)]  # exactly one bind, loopback only
    assert bound[0][1] is auth._Handler and recorder.closed


def test_production_default_port_is_the_registered_redirect_port():
    import inspect

    assert inspect.signature(CallbackServer).parameters["port"].default == CALLBACK_PORT == 5000
    assert REDIRECT_URI == f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"


def test_busy_default_port_fails_with_no_fallback_port(monkeypatch):
    import errno

    attempts = []

    def busy(address, handler):
        attempts.append(address)
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(auth, "_HTTPServer", busy)
    with pytest.raises(AuthFlowError, match=r"cannot listen on 127\.0\.0\.1:5000") as info:
        CallbackServer()
    assert attempts == [("127.0.0.1", 5000)]  # one attempt; never retried on another port
    assert "Address already in use" not in str(info.value)  # only the exception type is reported


def test_server_not_actually_bound_to_loopback_is_refused_and_closed(monkeypatch):
    recorder = _RecordingServer([], host_reported="0.0.0.0")
    monkeypatch.setattr(auth, "_HTTPServer", recorder)
    with pytest.raises(AuthFlowError, match="not bound to 127.0.0.1"):
        CallbackServer()
    assert recorder.closed


def test_host_cannot_be_chosen_and_port_cannot_be_shared():
    with pytest.raises(TypeError):
        CallbackServer(host="0.0.0.0")  # type: ignore[call-arg]
    with CallbackServer(port=0) as first:
        assert first._httpd.server_address[0] == "127.0.0.1"
        with pytest.raises(AuthFlowError):
            CallbackServer(port=first.port)


def test_valid_callback_succeeds_and_page_echoes_nothing():
    outcome, responses = serve_and_request([("GET", callback_target(STATE), None)])
    assert outcome.reveal() == AUTH_CODE
    status, headers, body = responses[0]
    assert status == 200 and b"complete" in body
    assert AUTH_CODE.encode() not in body and STATE.encode() not in body
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_wrong_state_fails_the_flow():
    outcome, responses = serve_and_request([("GET", callback_target("FORGED"), None)])
    assert isinstance(outcome, AuthFlowError) and "does not match" in str(outcome)
    assert responses[0][0] == 400 and AUTH_CODE.encode() not in responses[0][2]


def test_error_callback_fails_the_flow():
    outcome, _ = serve_and_request([("GET", f"/callback?error=access_denied&state={STATE}", None)])
    assert isinstance(outcome, AuthFlowError) and "access_denied" in str(outcome)


def test_unrelated_requests_are_ignored_and_the_flow_continues():
    outcome, responses = serve_and_request([
        ("GET", "/favicon.ico", None),                                   # browsers ask for this
        ("GET", callback_target(STATE), {"Host": "evil.example:5000"}),  # DNS-rebinding style Host
        ("POST", "/callback", None),
        ("GET", "/other?auth_code=" + AUTH_CODE + "&state=" + STATE, None),
        ("GET", callback_target(STATE), None),
    ])
    assert [r[0] for r in responses] == [404, 400, 405, 404, 200]
    assert outcome.reveal() == AUTH_CODE


def test_second_callback_after_completion_is_not_accepted():
    import http.client

    results = []
    with CallbackServer(port=0) as server:
        httpd = server._httpd
        httpd.expected_state, httpd.result, httpd.failure = STATE, None, None

        def browser():
            for code in (AUTH_CODE, "AUTHCODE.second.attempt1"):
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request("GET", callback_target(STATE, code), headers={"Host": f"127.0.0.1:{server.port}"})
                results.append(conn.getresponse().status)
                conn.close()

        thread = threading.Thread(target=browser, daemon=True)
        thread.start()
        httpd.handle_request()
        httpd.handle_request()  # serves the second request while the first result is still recorded
        thread.join(10)
        assert results == [200, 400]
        assert httpd.result.reveal() == AUTH_CODE  # the first code is kept; the second is refused


def test_timeout_raises():
    outcome, _ = serve_and_request([], timeout=0.3)
    assert isinstance(outcome, AuthFlowError) and "timed out" in str(outcome)


@pytest.mark.parametrize("timeout", [0, -1, 601])
def test_timeout_bounds(timeout):
    with CallbackServer(port=0) as server, pytest.raises(ValueError):
        server.wait_for_auth_code(STATE, timeout)


def test_server_writes_nothing_to_stdout_or_stderr(capfd):
    """http.server's default access log contains the request line, i.e. the auth code."""
    serve_and_request([("GET", "/favicon.ico", None), ("GET", callback_target(STATE), None)])
    captured = capfd.readouterr()
    assert AUTH_CODE not in captured.out + captured.err and STATE not in captured.out + captured.err
    assert captured.err == "" and captured.out == ""


# --- token exchange ----------------------------------------------------------------------------

def test_exchange_sends_documented_request_and_returns_masked_token():
    transport = ScriptedPost(token_resp())
    token = exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    (url, headers, body), = transport.calls
    assert url == "https://api-t1.fyers.in/api/v3/validate-authcode"
    assert body == {"grant_type": "authorization_code", "appIdHash": APP_ID_HASH, "code": AUTH_CODE}
    assert headers == {"Content-Type": "application/json"}  # no Authorization header on this call
    assert token.reveal() == ACCESS_TOKEN and ACCESS_TOKEN not in repr(token)
    assert APP_SECRET not in json.dumps(body)  # only the hash is sent


def test_exchange_accepts_success_payload_without_status_field():
    token = exchange_auth_code(creds(), SecretStr(AUTH_CODE),
                               transport=ScriptedPost(json_resp({"access_token": ACCESS_TOKEN})))
    assert token.reveal() == ACCESS_TOKEN


@pytest.mark.parametrize("response", [
    json_resp({"s": "error", "code": -5, "message": "invalid app id hash"}),
    json_resp({"s": "error", "code": -413, "message": "Invalid auth code"}, status=401),
    json_resp({"s": "ok", "code": 200, "message": ""}, status=500),
    HttpResponse(200, {}, b"<html>gateway</html>"),
    json_resp([1, 2]),
])
def test_exchange_rejections_raise_auth_error(response):
    transport = ScriptedPost(response)
    with pytest.raises(FyersAuthError):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    assert len(transport.calls) == 1  # the auth code is single-use: never retried


@pytest.mark.parametrize("token", [None, "", 42, "short", f"{APP_ID}:{ACCESS_TOKEN}", "has space " + ACCESS_TOKEN,
                                   ACCESS_TOKEN + "\n", "x" * 5000])
def test_unusable_access_tokens_are_refused(token):
    with pytest.raises(FyersApiError, match="no usable access_token"):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE),
                           transport=ScriptedPost(json_resp({"s": "ok", "access_token": token})))


def test_api_error_text_is_scrubbed_of_every_secret():
    echo = f"bad {APP_SECRET} hash {APP_ID_HASH} code {AUTH_CODE} app {APP_ID}"
    with pytest.raises(FyersAuthError) as info:
        exchange_auth_code(creds(), SecretStr(AUTH_CODE),
                           transport=ScriptedPost(json_resp({"s": "error", "code": -5, "message": echo})))
    text = str(info.value)
    for secret in (APP_SECRET, APP_ID_HASH, AUTH_CODE, APP_ID):
        assert secret not in text
    assert "code -5" in text


def test_network_error_propagates_without_retry():
    transport = ScriptedPost(FyersNetworkError("network error: TimeoutError"))
    with pytest.raises(FyersNetworkError):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    assert len(transport.calls) == 1


def test_exchange_requires_https():
    with pytest.raises(ValueError, match="https"):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=ScriptedPost(token_resp()),
                           base_url="http://api-t1.fyers.in/api/v3")


# --- transports never follow redirects (they would forward credential-bearing headers) ---------

@pytest.fixture
def redirecting_server():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _go(self):
            hits.append((self.command, self.path, self.headers.get("Authorization")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        do_GET = do_POST = _go

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], hits
    server.shutdown()
    server.server_close()


def test_get_transport_does_not_follow_redirects(redirecting_server):
    port, hits = redirecting_server
    response = urllib_transport(f"http://127.0.0.1:{port}/start", {"Authorization": "app:SECRETTOKEN"}, 5)
    assert response.status == 302
    assert [h[1] for h in hits] == ["/start"]  # /target was never requested, so no header was forwarded


def test_post_transport_does_not_follow_redirects(redirecting_server):
    port, hits = redirecting_server
    response = urllib_post_transport(f"http://127.0.0.1:{port}/start", {"Content-Type": "application/json"}, b"{}", 5)
    assert response.status == 302
    assert [h[1] for h in hits] == ["/start"]


# --- explicit callback port and redirect URI matching ------------------------------------------

@pytest.mark.parametrize("value, expected", [(1024, 1024), (5000, 5000), (5001, 5001), (65535, 65535),
                                             ("5001", 5001), (" 5001 ", 5001)])
def test_valid_callback_ports(value, expected):
    assert validate_callback_port(value) == expected


@pytest.mark.parametrize("value", [0, 1, 80, 1023, 65536, 99999, -1, "abc", "", "5000.5", "5 000", None, True, 5000.5])
def test_invalid_callback_ports_are_refused(value):
    with pytest.raises(AuthFlowError, match="callback port"):
        validate_callback_port(value)


def test_redirect_uri_format_and_default():
    assert redirect_uri_for(5001) == "http://127.0.0.1:5001/callback"
    assert redirect_uri_for(5000) == REDIRECT_URI == "http://127.0.0.1:5000/callback"


def test_login_url_uses_the_given_redirect_uri_and_defaults_to_the_registered_one():
    custom = redirect_uri_for(5001)
    parsed = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(build_login_url(APP_ID, STATE, redirect_uri=custom)).query))
    assert parsed["redirect_uri"] == custom and parsed["state"] == STATE
    default = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(build_login_url(APP_ID, STATE)).query))
    assert default["redirect_uri"] == "http://127.0.0.1:5000/callback"


@pytest.mark.parametrize("port", [5000, 5001, 8765, 65535])
def test_custom_port_binds_only_127_0_0_1_on_exactly_that_port(monkeypatch, port):
    bound = []
    recorder = _RecordingServer(bound)
    monkeypatch.setattr(auth, "_HTTPServer", recorder)
    with CallbackServer(port=port) as server:
        assert server.port == port and server.redirect_uri == f"http://127.0.0.1:{port}/callback"
        assert build_login_url(APP_ID, STATE, redirect_uri=server.redirect_uri).count(f"%3A{port}%2Fcallback") == 1
    assert [address for address, _ in bound] == [("127.0.0.1", port)]  # one bind, loopback only


def test_redirect_uri_comes_from_the_port_really_bound():
    with CallbackServer(port=0) as server:  # OS picks the port; the URI must still name it exactly
        assert server.port not in (0, 5000) or server.port == 5000
        assert server.redirect_uri == f"http://127.0.0.1:{server.port}/callback"


def test_bind_failure_on_custom_port_has_no_fallback_and_explains_the_dashboard(monkeypatch):
    import errno

    attempts = []

    def busy(address, handler):
        attempts.append(address)
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(auth, "_HTTPServer", busy)
    with pytest.raises(AuthFlowError, match=r"cannot listen on 127\.0\.0\.1:5001") as info:
        CallbackServer(port=5001)
    message = str(info.value)
    assert attempts == [("127.0.0.1", 5001)]
    assert "FYERS_CALLBACK_PORT" in message and "--port" in message and "FYERS dashboard" in message
    assert "Address already in use" not in message


def test_real_busy_port_is_refused_not_shared_and_not_replaced():
    import socket

    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        with pytest.raises(AuthFlowError, match=f"127.0.0.1:{taken}"):
            CallbackServer(port=taken)
    finally:
        blocker.close()


def test_state_is_still_strictly_validated_on_a_non_default_port():
    outcome, responses = serve_and_request([("GET", callback_target("FORGED"), None)])
    assert isinstance(outcome, AuthFlowError) and "does not match" in str(outcome) and responses[0][0] == 400
