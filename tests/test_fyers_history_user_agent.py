"""EXPERIMENTAL opt-in --user-agent for the historical-data downloader: default unchanged, safe validation."""

import http.server
import json
import re
import threading
from datetime import date

import pytest

from nifty_signal_engine.fyers import AuthFlowError, FyersHistoryClient, FyersRateLimitError, urllib_transport
from nifty_signal_engine.fyers import auth, client as client_module
from nifty_signal_engine.fyers.__main__ import main
from nifty_signal_engine.synthetic import generate_synthetic_candles

from fyers_helpers import (
    FAKE_APP_ID,
    FAKE_TOKEN,
    NOW,
    FakeFyersApi,
    ScriptedTransport,
    fake_credentials,
    json_response,
    make_client,
    ok_payload,
    query,
)

UA = "nifty-signal-engine-experiment/0.1"
ROW = [1704167100, 100.0, 101.0, 99.0, 100.5, 0]


def fetch(client):
    return client.fetch_history("NSE:NIFTY50-INDEX", "5", 1704067200, 1704153599)


def first_call(**client_kwargs):
    transport = ScriptedTransport(json_response(ok_payload([ROW])))
    client, _ = make_client(transport, **client_kwargs)
    fetch(client)
    return transport.calls[0]


# --- headers handed to the transport ------------------------------------------------------------

def test_default_headers_are_exactly_the_original_three():
    url, headers = first_call()
    assert headers == {"Authorization": f"{FAKE_APP_ID}:{FAKE_TOKEN}", "Content-Type": "application/json",
                       "version": "3"}
    assert "user-agent" not in {k.lower() for k in headers}


def test_explicit_none_equals_the_default():
    assert first_call(user_agent=None) == first_call()


def test_override_adds_only_the_user_agent_header():
    default_url, default_headers = first_call()
    url, headers = first_call(user_agent=UA)
    assert headers == {**default_headers, "User-Agent": UA}
    assert url == default_url and query(url)["symbol"] == "NSE:NIFTY50-INDEX"  # same request otherwise
    assert FAKE_TOKEN not in url  # the token still travels only in the header


def test_override_is_trimmed():
    assert first_call(user_agent=f"  {UA}\t ")[1]["User-Agent"] == UA


def test_the_validator_is_the_same_function_the_token_exchange_uses():
    assert auth.validate_user_agent is client_module.validate_user_agent


@pytest.mark.parametrize("value", [
    "", "   ", "\t",
    "agent\r\nX-Injected: 1", "agent\nX-Injected: 1", "agent\rX", "a\x00b", "a\x1b[31m", "tab\there",
    "café", "日本", "x" * 201,
    42, b"bytes", ["list"],
])
def test_unsafe_values_are_refused_at_construction_before_any_request(value):
    transport = ScriptedTransport(json_response(ok_payload([ROW])))
    with pytest.raises(AuthFlowError, match="user agent"):
        FyersHistoryClient(fake_credentials(), transport=transport, user_agent=value)
    assert transport.calls == []


@pytest.mark.parametrize("value", [
    "nifty-signal-engine/0.1", "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "python-requests/2.32.3", "x" * 200,
])
def test_valid_values_are_accepted(value):
    assert first_call(user_agent=value)[1]["User-Agent"] == value


# --- retry behaviour is untouched ---------------------------------------------------------------

@pytest.mark.parametrize("user_agent", [None, UA])
def test_rate_limit_backoff_is_identical_with_or_without_the_override(user_agent):
    transport = ScriptedTransport(
        json_response({"s": "error", "code": 429, "message": "slow down"}, status=429),
        json_response({"s": "error", "code": -429, "message": "rate limit"}),
        json_response(ok_payload([ROW])),
    )
    client, sleeps = make_client(transport, backoff_seconds=2.0, user_agent=user_agent)
    assert fetch(client)["candles"] == [ROW]
    assert sleeps == [2.0, 4.0] and len(transport.calls) == 3
    assert all(("User-Agent" in headers) == (user_agent is not None) for _, headers in transport.calls)


@pytest.mark.parametrize("user_agent", [None, UA])
def test_retries_are_exhausted_the_same_way(user_agent):
    transport = ScriptedTransport(json_response({"s": "error", "code": 429, "message": "slow"}, status=429))
    client, sleeps = make_client(transport, max_retries=2, user_agent=user_agent)
    with pytest.raises(FyersRateLimitError):
        fetch(client)
    assert len(transport.calls) == 3 and len(sleeps) == 2


# --- what really goes on the wire (loopback only; the real urllib transport) -------------------

@pytest.fixture
def wire():
    captured = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append({"path": self.path, "headers": [(k, v) for k, v in self.headers.items()]})
            body = json.dumps(ok_payload([ROW])).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def transport(url, headers, timeout):
        assert url.startswith("https://api-t1.fyers.in/data/history?")  # the client still targets FYERS
        return urllib_transport(f"http://127.0.0.1:{port}" + url.split("fyers.in", 1)[1], headers, timeout)

    yield transport, captured
    server.shutdown()
    server.server_close()


def agents(request):
    return [v for k, v in request["headers"] if k.lower() == "user-agent"]


def others(request):
    return sorted((k.lower(), v) for k, v in request["headers"] if k.lower() not in ("user-agent", "host"))


def test_on_the_wire_default_is_still_urllibs_own_user_agent(wire):
    transport, captured = wire
    fetch(make_client(transport)[0])
    (request,) = captured
    assert len(agents(request)) == 1 and re.fullmatch(r"Python-urllib/\d+\.\d+", agents(request)[0])


def test_on_the_wire_override_replaces_it_exactly_once_and_nothing_else_changes(wire):
    transport, captured = wire
    fetch(make_client(transport)[0])
    fetch(make_client(transport, user_agent=UA)[0])
    default, override = captured
    assert agents(override) == [UA] and not any("Python-urllib" in v for _, v in override["headers"])
    assert default["path"] == override["path"]            # same query string: symbol, resolution, range, flags
    assert others(default) == others(override)            # Authorization, Content-Type, version, ... identical


# --- the downloader command ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def candles():
    return generate_synthetic_candles(5, start=date(2024, 1, 8), seed=2)


def run_cli(tmp_path, api, *argv):
    env = {"NIFTY_DATA_DIR": str(tmp_path / "data"), "FYERS_APP_ID": FAKE_APP_ID, "FYERS_ACCESS_TOKEN": FAKE_TOKEN}
    return main(["--min-interval", "0", "--start", "2024-01-08", "--end", "2024-01-12", *argv], env=env,
                transport=api, sleep=lambda s: None, now=NOW, dotenv_path=tmp_path / "absent.env")


def test_cli_default_sends_no_user_agent_and_prints_no_note(tmp_path, capsys, candles):
    api = FakeFyersApi(candles)
    assert run_cli(tmp_path, api) == 0
    out = capsys.readouterr().out
    assert all("User-Agent" not in headers for _, headers in api.calls) and "EXPERIMENTAL" not in out
    assert "wrote 375 candles" in out


def test_cli_override_is_sent_on_every_history_request_and_announced(tmp_path, capsys, candles):
    api = FakeFyersApi(candles)
    assert run_cli(tmp_path, api, "--user-agent", UA) == 0
    out = capsys.readouterr()
    assert api.calls and all(headers["User-Agent"] == UA for _, headers in api.calls)
    assert "EXPERIMENTAL User-Agent override" in out.out and "not a fix" in out.out and UA in out.out
    assert "wrote 375 candles" in out.out
    for text in (out.out, out.err):
        assert FAKE_TOKEN not in text and FAKE_APP_ID not in text


def test_cli_override_applies_to_each_window_of_a_long_range(tmp_path, candles):
    api = FakeFyersApi(candles)
    env = {"NIFTY_DATA_DIR": str(tmp_path / "data"), "FYERS_APP_ID": FAKE_APP_ID, "FYERS_ACCESS_TOKEN": FAKE_TOKEN}
    code = main(["--min-interval", "0", "--start", "2024-01-01", "--end", "2024-06-30", "--window-days", "30",
                 "--user-agent", UA], env=env, transport=api, sleep=lambda s: None, now=NOW,
                dotenv_path=tmp_path / "absent.env")
    assert code == 0 and len(api.calls) > 3
    assert {headers["User-Agent"] for _, headers in api.calls} == {UA}


@pytest.mark.parametrize("value", ["bad\r\nX-Injected: 1", "café", "x" * 201, "   "])
def test_cli_rejects_an_unsafe_value_before_any_request(tmp_path, capsys, candles, value):
    api = FakeFyersApi(candles)
    assert run_cli(tmp_path, api, "--user-agent", value) == 1
    assert "user agent" in capsys.readouterr().err
    assert api.calls == [] and not (tmp_path / "data").exists()  # nothing requested, nothing written


def test_cli_override_does_not_leak_into_any_output_file(tmp_path, candles):
    run_cli(tmp_path, FakeFyersApi(candles), "--user-agent", UA)
    files = [p for p in (tmp_path / "data").rglob("*") if p.is_file()]
    assert len(files) >= 3
    for path in files:
        text = path.read_text()
        assert FAKE_TOKEN not in text and FAKE_APP_ID not in text and "Authorization" not in text


def test_cli_a_rejected_request_with_the_override_still_exits_cleanly(tmp_path, capsys):
    transport = ScriptedTransport(json_response({"s": "error", "code": -16, "message": "Could not authenticate"}))
    assert run_cli(tmp_path, transport, "--user-agent", UA) == 1
    out = capsys.readouterr()
    assert "authentication failed" in out.err and len(transport.calls) == 1  # auth errors are never retried
    assert FAKE_TOKEN not in out.out + out.err
