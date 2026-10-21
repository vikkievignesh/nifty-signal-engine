import http.client
import logging
import os
import stat
import threading
import urllib.parse

import pytest

from nifty_signal_engine.fyers import AuthFlowError, CallbackServer, FyersNetworkError, load_credentials
from nifty_signal_engine.fyers.login import main

from fyers_auth_helpers import (
    ACCESS_TOKEN,
    APP_ID,
    APP_ID_HASH,
    APP_SECRET,
    AUTH_CODE,
    ScriptedPost,
    json_resp,
    token_resp,
)

ENV_TEXT = (
    "# my settings\n"
    f"FYERS_APP_ID={APP_ID}\n"
    f"FYERS_APP_SECRET={APP_SECRET}\n"
    "NIFTY_LOG_LEVEL=DEBUG\n"
    "FYERS_ACCESS_TOKEN=old_TOKEN_0123456789abcdef\n"
)
SECRETS = (APP_SECRET, APP_ID_HASH, AUTH_CODE, ACCESS_TOKEN)


class Harness:
    """Fake browser + loopback server on an ephemeral port; nothing leaves 127.0.0.1."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.server = None
        self.opened_urls = []
        self.server_created = False

    def server_factory(self, port):
        self.requested_port = port  # the real port is ephemeral here; the login URL must follow the bound server
        self.server_created = True
        self.server = CallbackServer(port=0)
        return self.server

    def open_url(self, url):
        self.opened_urls.append(url)
        if self.behaviour == "no-browser":
            return False
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        if self.behaviour == "silent":
            return True
        params = {"ok": {"auth_code": AUTH_CODE, "state": state},
                  "wrong-state": {"auth_code": AUTH_CODE, "state": "FORGED"},
                  "denied": {"error": "access_denied", "state": state},
                  "no-code": {"state": state}}[self.behaviour]

        def visit():
            conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
            conn.request("GET", "/callback?" + urllib.parse.urlencode(params),
                         headers={"Host": f"127.0.0.1:{self.server.port}"})
            conn.getresponse().read()
            conn.close()

        threading.Thread(target=visit, daemon=True).start()
        return True


def run(tmp_path, harness, transport, *extra, env=None, env_text=ENV_TEXT):
    env_file = tmp_path / ".env"
    if env_text is not None:
        env_file.write_text(env_text)
        env_file.chmod(0o644)  # deliberately loose: the helper must tighten it
    code = main(["--env-file", str(env_file), *extra], env={} if env is None else env,
                open_url=harness.open_url, post_transport=transport, server_factory=harness.server_factory)
    return code, env_file


def assert_no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def test_successful_login_updates_only_the_token(tmp_path, capsys, caplog):
    transport = ScriptedPost(token_resp())
    with caplog.at_level(logging.DEBUG):
        code, env_file = run(tmp_path, Harness("ok"), transport)
    out = capsys.readouterr()
    assert code == 0
    assert env_file.read_text() == ENV_TEXT.replace("old_TOKEN_0123456789abcdef", ACCESS_TOKEN)
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    (url, headers, body), = transport.calls
    assert body == {"grant_type": "authorization_code", "appIdHash": APP_ID_HASH, "code": AUTH_CODE}
    assert "updated" in out.out and "value not shown" in out.out
    assert_no_secrets(out.out, out.err, caplog.text)
    # the downloader can use the result straight away
    assert load_credentials({}, env_file).access_token == ACCESS_TOKEN


def test_credentials_can_come_from_the_environment(tmp_path):
    env = {"FYERS_APP_ID": APP_ID, "FYERS_APP_SECRET": APP_SECRET}
    code, env_file = run(tmp_path, Harness("ok"), ScriptedPost(token_resp()), env=env, env_text="OTHER=1\n")
    assert code == 0 and env_file.read_text() == f"OTHER=1\nFYERS_ACCESS_TOKEN={ACCESS_TOKEN}\n"
    assert "FYERS_APP_SECRET" not in env_file.read_text()  # secrets from the environment are not copied to disk


def test_state_mismatch_aborts_without_touching_env_or_exchanging(tmp_path, capsys):
    transport = ScriptedPost(token_resp())
    code, env_file = run(tmp_path, Harness("wrong-state"), transport)
    err = capsys.readouterr().err
    assert code == 1 and "does not match" in err
    assert transport.calls == []  # the forged code was never exchanged
    assert env_file.read_text() == ENV_TEXT
    assert_no_secrets(err)


@pytest.mark.parametrize("behaviour, expect", [("denied", "access_denied"), ("no-code", "did not return")])
def test_error_callbacks_abort(tmp_path, capsys, behaviour, expect):
    transport = ScriptedPost(token_resp())
    code, env_file = run(tmp_path, Harness(behaviour), transport)
    assert code == 1 and expect in capsys.readouterr().err
    assert transport.calls == [] and env_file.read_text() == ENV_TEXT


@pytest.mark.parametrize("response", [
    json_resp({"s": "error", "code": -5, "message": f"invalid app id hash {APP_ID_HASH}"}),
    json_resp({"s": "error", "code": -413, "message": "bad code"}, status=401),
    json_resp({"s": "ok", "access_token": "tiny"}),
])
def test_exchange_failures_leave_env_untouched_and_leak_nothing(tmp_path, capsys, caplog, response):
    with caplog.at_level(logging.DEBUG):
        code, env_file = run(tmp_path, Harness("ok"), ScriptedPost(response))
    out = capsys.readouterr()
    assert code == 1 and env_file.read_text() == ENV_TEXT
    assert_no_secrets(out.out, out.err, caplog.text)


def test_network_failure_during_exchange(tmp_path, capsys):
    code, env_file = run(tmp_path, Harness("ok"), ScriptedPost(FyersNetworkError("network error: TimeoutError")))
    assert code == 1 and "network error" in capsys.readouterr().err
    assert env_file.read_text() == ENV_TEXT


def test_missing_credentials_fail_before_any_server_or_browser(tmp_path, capsys):
    harness = Harness("ok")
    code, _ = run(tmp_path, harness, ScriptedPost(token_resp()), env_text=f"FYERS_APP_ID={APP_ID}\n")
    err = capsys.readouterr().err
    assert code == 1 and "FYERS_APP_SECRET" in err and APP_ID not in err
    assert not harness.server_created and harness.opened_urls == []


def test_symlinked_env_fails_before_any_server_or_browser(tmp_path, capsys):
    real = tmp_path / "real.txt"
    real.write_text(ENV_TEXT)
    link = tmp_path / ".env"
    link.symlink_to(real)
    harness = Harness("ok")
    code = main(["--env-file", str(link)], env={}, open_url=harness.open_url,
                post_transport=ScriptedPost(token_resp()), server_factory=harness.server_factory)
    assert code == 1 and "symbolic link" in capsys.readouterr().err
    assert not harness.server_created and real.read_text() == ENV_TEXT


def test_port_in_use_is_reported(tmp_path, capsys):
    def busy(port):
        raise AuthFlowError("cannot listen on 127.0.0.1:5000 (OSError); is another program using that port?")

    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT)
    code = main(["--env-file", str(env_file)], env={}, open_url=lambda u: True,
                post_transport=ScriptedPost(token_resp()), server_factory=busy)
    assert code == 1 and "cannot listen" in capsys.readouterr().err


def test_timeout(tmp_path, capsys):
    code, env_file = run(tmp_path, Harness("silent"), ScriptedPost(token_resp()), "--timeout", "0.3")
    assert code == 1 and "timed out" in capsys.readouterr().err and env_file.read_text() == ENV_TEXT


def test_browser_failure_aborts_unless_url_is_printed(tmp_path, capsys):
    code, _ = run(tmp_path, Harness("no-browser"), ScriptedPost(token_resp()))
    assert code == 1 and "--print-url" in capsys.readouterr().err


def test_login_url_is_hidden_by_default_and_shown_only_on_request(tmp_path, capsys):
    harness = Harness("ok")
    run(tmp_path, harness, ScriptedPost(token_resp()))
    hidden = capsys.readouterr().out
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(harness.opened_urls[0]).query)["state"][0]
    assert state not in hidden and "client_id" not in hidden

    harness = Harness("ok")
    run(tmp_path, harness, ScriptedPost(token_resp()), "--print-url")
    shown = capsys.readouterr().out
    assert "client_id=" in shown and "state=" in shown
    assert_no_secrets(shown)


def test_no_browser_requires_print_url(tmp_path, capsys):
    harness = Harness("ok")
    code, _ = run(tmp_path, harness, ScriptedPost(token_resp()), "--no-browser")
    assert code == 1 and harness.opened_urls == []


def test_ctrl_c_is_a_clean_cancel(tmp_path, capsys):
    def interrupted(port):
        raise KeyboardInterrupt

    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT)
    code = main(["--env-file", str(env_file)], env={}, open_url=lambda u: True,
                post_transport=ScriptedPost(token_resp()), server_factory=interrupted)
    assert code == 130 and "cancelled" in capsys.readouterr().err


def test_downloader_credential_loading_is_unaffected_by_the_secret(tmp_path):
    """The downloader never reads FYERS_APP_SECRET, even when it is in the same .env."""
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT)
    env_file.chmod(0o600)
    creds = load_credentials({}, env_file)
    assert APP_SECRET not in repr(creds) and creds.app_id == APP_ID


# --- explicit callback port ---------------------------------------------------------------------

class StubServer:
    """Looks like CallbackServer but never binds; stops the flow right after the login URL is built."""

    def __init__(self, port):
        self.port = port
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def wait_for_auth_code(self, state, timeout):
        raise AuthFlowError("stub: stop after the login URL")


def run_with_stub(tmp_path, *argv, env=None, env_text=ENV_TEXT):
    requested, urls = [], []

    def factory(port):
        requested.append(port)
        return StubServer(port)

    env_file = tmp_path / ".env"
    env_file.write_text(env_text)
    env_file.chmod(0o600)
    code = main(["--env-file", str(env_file), *argv], env={} if env is None else env,
                open_url=lambda url: urls.append(url) or True,
                post_transport=ScriptedPost(token_resp()), server_factory=factory)
    return code, requested, urls


def redirect_param(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]


def test_default_port_is_5000_and_login_url_uses_the_registered_redirect_uri(tmp_path, capsys):
    code, requested, urls = run_with_stub(tmp_path)
    out = capsys.readouterr().out
    assert code == 1 and requested == [5000]  # the stub stops the flow; we only inspect what was set up
    assert redirect_param(urls[0]) == "http://127.0.0.1:5000/callback"
    assert "NOTE: custom port" not in out


@pytest.mark.parametrize("argv, env, env_text, expected", [
    (["--port", "5001"], {}, ENV_TEXT, 5001),
    ([], {"FYERS_CALLBACK_PORT": "5002"}, ENV_TEXT, 5002),
    ([], {}, ENV_TEXT + "FYERS_CALLBACK_PORT=5003\n", 5003),
    (["--port", "5004"], {"FYERS_CALLBACK_PORT": "5002"}, ENV_TEXT, 5004),   # CLI beats the environment
    ([], {"FYERS_CALLBACK_PORT": "5002"}, ENV_TEXT + "FYERS_CALLBACK_PORT=5003\n", 5002),  # env beats .env
])
def test_custom_port_is_passed_to_the_server_and_matches_the_login_url(tmp_path, capsys, argv, env, env_text, expected):
    code, requested, urls = run_with_stub(tmp_path, *argv, env=env, env_text=env_text)
    out = capsys.readouterr().out
    exact = f"http://127.0.0.1:{expected}/callback"
    assert requested == [expected]                       # exactly one server, on exactly the configured port
    assert redirect_param(urls[0]) == exact              # login URL == listening URI == what must be registered
    assert f"Listening on {exact}" in out
    assert f"NOTE: custom port {expected}" in out and "FYERS dashboard" in out and "save it" in out
    assert APP_SECRET not in out


@pytest.mark.parametrize("value", ["0", "80", "1023", "65536", "-5", "abc", "50 01", "5000.5"])
def test_invalid_ports_are_rejected_before_any_server_or_browser(tmp_path, capsys, value):
    code, requested, urls = run_with_stub(tmp_path, "--port", value)
    err = capsys.readouterr().err
    assert code == 1 and "callback port" in err  # same message whether it came from --port or the environment
    assert requested == [] and urls == []


def test_invalid_port_from_environment_is_rejected(tmp_path, capsys):
    code, requested, _ = run_with_stub(tmp_path, env={"FYERS_CALLBACK_PORT": "not-a-port"})
    assert code == 1 and "callback port must be an integer" in capsys.readouterr().err and requested == []


def test_login_url_follows_the_port_the_server_really_bound(tmp_path):
    """Integration: real CallbackServer on an ephemeral port; the URL must name that exact port."""
    harness = Harness("ok")
    code, _ = run(tmp_path, harness, ScriptedPost(token_resp()), "--port", "5001")
    assert code == 0
    assert redirect_param(harness.opened_urls[0]) == f"http://127.0.0.1:{harness.server.port}/callback"
    assert harness.requested_port == 5001


def test_busy_custom_port_fails_once_without_fallback_and_leaves_env_untouched(tmp_path, capsys):
    calls = []

    def busy(port):
        calls.append(port)
        raise AuthFlowError(f"cannot listen on 127.0.0.1:{port} (OSError); another program is probably using "
                            "that port.")

    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT)
    opened = []
    code = main(["--env-file", str(env_file), "--port", "5001"], env={}, open_url=opened.append,
                post_transport=ScriptedPost(token_resp()), server_factory=busy)
    assert code == 1 and calls == [5001]  # one attempt on the requested port only
    assert "127.0.0.1:5001" in capsys.readouterr().err
    assert opened == [] and env_file.read_text() == ENV_TEXT


# --- network failures during the token exchange (diagnostics) ---------------------------------

def _network_failure(kind):
    import errno
    import ssl
    import urllib.error

    from nifty_signal_engine.fyers.netdiag import network_error_from

    if kind == "certificate":
        cert = ssl.SSLCertVerificationError(1, f"raw text with {AUTH_CODE} and {APP_SECRET}")
        cert.verify_code = 20
        return network_error_from(urllib.error.URLError(cert))
    if kind == "timeout":
        return network_error_from(TimeoutError(f"raw {AUTH_CODE}"))
    return network_error_from(urllib.error.URLError(OSError(errno.ECONNREFUSED, f"raw {APP_ID_HASH}")))


@pytest.mark.parametrize("kind, category, must_say", [
    ("certificate", "tls_certificate", ["Install Certificates.command", "auth code was not used", "diagnose"]),
    ("timeout", "timeout", ["may already be used up", "Run the login again"]),
    ("refused", "connection", ["ECONNREFUSED", "auth code was not used"]),
])
def test_exchange_network_failures_are_classified_actionable_and_leak_nothing(tmp_path, capsys, caplog, kind,
                                                                              category, must_say):
    transport = ScriptedPost(_network_failure(kind))
    with caplog.at_level(logging.DEBUG):
        code, env_file = run(tmp_path, Harness("ok"), transport)
    out = capsys.readouterr()
    assert code == 1 and len(transport.calls) == 1  # the single-use code is never retried
    assert f"network error [{category}]" in out.err
    for phrase in must_say:
        assert phrase in out.err
    assert env_file.read_text() == ENV_TEXT  # nothing was saved
    assert_no_secrets(out.out, out.err, caplog.text)


# --- HTTP 403 during the token exchange (safe diagnostics) ------------------------------------

@pytest.mark.parametrize("response, expect, absent", [
    (json_resp({"s": "error", "code": -5, "message": f"invalid app id hash {APP_ID_HASH}",
                "debug": "internal-detail-0123456789abcdef"}, status=403),
     ['API code -5', 'message "invalid app id hash ***"'], ["internal-detail", "debug"]),
    (__import__("nifty_signal_engine.fyers", fromlist=["HttpResponse"]).HttpResponse(
        403, {"Content-Type": "text/html"}, b"<html><title>Attention Required</title>cf-ray 8a1b</html>"),
     ["an HTML page, not a JSON API error", "text/html"], ["Attention", "cf-ray"]),
])
def test_http_403_during_login_is_explained_safely_and_nothing_is_saved(tmp_path, capsys, caplog, response,
                                                                         expect, absent):
    transport = ScriptedPost(response)
    with caplog.at_level(logging.DEBUG):
        code, env_file = run(tmp_path, Harness("ok"), transport)
    out = capsys.readouterr()
    assert code == 1 and len(transport.calls) == 1
    assert "token exchange rejected (HTTP 403)" in out.err
    for phrase in expect:
        assert phrase in out.err
    for fragment in absent:
        assert fragment not in out.err
    assert env_file.read_text() == ENV_TEXT
    assert_no_secrets(out.out, out.err, caplog.text)
