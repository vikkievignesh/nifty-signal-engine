"""EXPERIMENTAL opt-in User-Agent for the token exchange: sent only when configured; default unchanged."""

import http.server
import json
import re
import threading

import pytest

from nifty_signal_engine.fyers import AuthFlowError, FyersAuthError, FyersNetworkError, SecretStr, exchange_auth_code
from nifty_signal_engine.fyers.auth import describe_exchange_request, urllib_post_transport, validate_user_agent

from fyers_auth_helpers import ACCESS_TOKEN, APP_ID_HASH, AUTH_CODE, ScriptedPost, creds, json_resp, token_resp

UA = "nifty-signal-engine-experiment/0.1"


# --- header handed to the transport -------------------------------------------------------------

def run_exchange(**kwargs):
    transport = ScriptedPost(token_resp())
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, **kwargs)
    return transport.calls[0]


def test_default_sets_no_user_agent_so_the_current_behaviour_is_unchanged():
    url, headers, body = run_exchange()
    assert headers == {"Content-Type": "application/json"}
    assert "User-Agent" not in headers and "user-agent" not in {k.lower() for k in headers}


def test_explicit_none_is_the_same_as_the_default():
    assert run_exchange(user_agent=None) == run_exchange()


def test_configured_user_agent_is_added_and_nothing_else_changes():
    default_url, default_headers, default_body = run_exchange()
    url, headers, body = run_exchange(user_agent=UA)
    assert headers == {"Content-Type": "application/json", "User-Agent": UA}
    assert (url, body) == (default_url, default_body)             # same endpoint, same JSON body
    assert set(body) == {"grant_type", "appIdHash", "code"} and body["appIdHash"] == APP_ID_HASH


def test_user_agent_is_trimmed():
    assert run_exchange(user_agent=f"  {UA}\t ")[1]["User-Agent"] == UA  # surrounding whitespace only


@pytest.mark.parametrize("value", [
    "nifty-signal-engine/0.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "python-requests/2.32.3",
    "a",
    "x" * 200,
])
def test_valid_user_agents_are_accepted(value):
    assert validate_user_agent(value) == value


@pytest.mark.parametrize("value", [
    "", "   ", "\t",
    "agent\r\nX-Injected: 1", "agent\nX-Injected: 1", "agent\rX", "a\x00b", "a\x1b[31m", "tab\there",
    "café", "日本", "x" * 201,
    42, b"bytes", ["list"],   # (None is not here: it means "no override", covered above)
])
def test_unsafe_user_agents_are_refused_before_anything_is_sent(value):
    transport = ScriptedPost(token_resp())
    with pytest.raises(AuthFlowError, match="user agent"):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, user_agent=value)
    assert transport.calls == []


def test_a_rejected_exchange_with_an_override_leaks_nothing_and_is_not_retried():
    transport = ScriptedPost(json_resp({"s": "error", "code": -5, "message": f"bad {AUTH_CODE}"}, status=403))
    with pytest.raises(FyersAuthError) as info:
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, user_agent=UA)
    assert len(transport.calls) == 1 and AUTH_CODE not in str(info.value)


def test_network_failures_with_an_override_are_still_never_retried():
    transport = ScriptedPost(FyersNetworkError("network error [timeout]: x", category="timeout"))
    with pytest.raises(FyersNetworkError):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, user_agent=UA)
    assert len(transport.calls) == 1


# --- what really goes on the wire (loopback only; the real urllib transport) ------------------

@pytest.fixture
def wire():
    captured = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            captured.append({"path": self.path, "headers": [(k, v) for k, v in self.headers.items()], "body": body})
            payload = json.dumps({"s": "ok", "access_token": ACCESS_TOKEN}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def transport(url, headers, body, timeout):
        # exchange_auth_code insists on https; deliver the very same headers/body to the loopback recorder
        assert url == "https://api-t1.fyers.in/api/v3/validate-authcode"
        return urllib_post_transport(f"http://127.0.0.1:{port}/api/v3/validate-authcode", headers, body, timeout)

    yield transport, captured
    server.shutdown()
    server.server_close()


def user_agents(request):
    return [v for k, v in request["headers"] if k.lower() == "user-agent"]


def other_headers(request):
    return sorted((k.lower(), v) for k, v in request["headers"] if k.lower() not in ("user-agent", "host"))


def test_on_the_wire_the_default_is_still_urllibs_own_user_agent(wire):
    transport, captured = wire
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    (request,) = captured
    assert len(user_agents(request)) == 1 and re.fullmatch(r"Python-urllib/\d+\.\d+", user_agents(request)[0])


def test_on_the_wire_the_configured_user_agent_replaces_it_exactly_once(wire):
    transport, captured = wire
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, user_agent=UA)
    (request,) = captured
    assert user_agents(request) == [UA]                                   # one header, our value
    assert not any("Python-urllib" in v for _, v in request["headers"])


def test_on_the_wire_only_the_user_agent_differs_between_default_and_override(wire):
    transport, captured = wire
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport, user_agent=UA)
    default, override = captured
    assert default["path"] == override["path"] == "/api/v3/validate-authcode"
    assert default["body"] == override["body"]                            # request body byte-for-byte identical
    assert other_headers(default) == other_headers(override)              # Content-Type, Content-Length, ... same
    assert user_agents(default) != user_agents(override)


def test_a_crlf_value_never_reaches_the_wire(wire):
    transport, captured = wire
    with pytest.raises(AuthFlowError):
        exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport,
                           user_agent="ok\r\nX-Injected: yes")
    assert captured == []


# --- --check output ----------------------------------------------------------------------------

def test_describe_exchange_request_shows_the_default_and_the_override():
    default = "\n".join(describe_exchange_request(creds(), 5000))
    assert "User-Agent  : the urllib default, Python-urllib/<python version> (no override)" in default
    override = "\n".join(describe_exchange_request(creds(), 5000, UA))
    assert f"User-Agent  : {UA}   (EXPERIMENTAL override, set with --user-agent)" in override
