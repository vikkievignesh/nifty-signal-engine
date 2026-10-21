import logging

import pytest

from nifty_signal_engine.fyers import (
    FyersApiError,
    FyersAuthError,
    FyersHistoryClient,
    FyersNetworkError,
    FyersRateLimitError,
    HttpResponse,
)

from fyers_helpers import (
    FAKE_APP_ID,
    FAKE_TOKEN,
    ScriptedTransport,
    fake_credentials,
    json_response,
    make_client,
    ok_payload,
    query,
)

ROW = [1704167100, 100.0, 101.0, 99.0, 100.5, 0]


def fetch(client):
    return client.fetch_history("NSE:NIFTY50-INDEX", "5", 1704067200, 1704153599)


def test_request_matches_documented_shape():
    transport = ScriptedTransport(json_response(ok_payload([ROW])))
    client, _ = make_client(transport)
    payload = fetch(client)
    assert payload["candles"] == [ROW]
    (url, headers), = transport.calls
    assert url.startswith("https://api-t1.fyers.in/data/history?")
    assert query(url) == {"symbol": "NSE:NIFTY50-INDEX", "resolution": "5", "date_format": "0",
                          "range_from": "1704067200", "range_to": "1704153599", "cont_flag": "1"}
    assert headers["Authorization"] == f"{FAKE_APP_ID}:{FAKE_TOKEN}"
    assert headers["version"] == "3"
    assert FAKE_TOKEN not in url  # the token travels only in the header


def test_no_data_payload_is_returned_not_raised():
    client, _ = make_client(ScriptedTransport(json_response({"s": "no_data", "code": 200, "candles": []})))
    assert fetch(client)["s"] == "no_data"


def test_rejects_non_https_base_url():
    with pytest.raises(ValueError, match="https"):
        FyersHistoryClient(fake_credentials(), base_url="http://api-t1.fyers.in/data")


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_failure_is_not_retried(status):
    transport = ScriptedTransport(json_response({"s": "error", "code": -16, "message": "nope"}, status=status))
    client, sleeps = make_client(transport)
    with pytest.raises(FyersAuthError, match="Regenerate the access token"):
        fetch(client)
    assert len(transport.calls) == 1 and sleeps == []


@pytest.mark.parametrize("payload", [
    {"s": "error", "code": -16, "message": "Could not authenticate the user"},
    {"s": "error", "code": -8, "message": "x"},
    {"s": "error", "code": 0, "message": "Your token has expired"},
])
def test_auth_failure_inside_http_200_body(payload):
    transport = ScriptedTransport(json_response(payload))
    client, _ = make_client(transport)
    with pytest.raises(FyersAuthError):
        fetch(client)
    assert len(transport.calls) == 1


def test_rate_limit_retries_with_backoff_then_succeeds():
    transport = ScriptedTransport(
        json_response({"s": "error", "code": 429, "message": "slow down"}, status=429),
        json_response({"s": "error", "code": -429, "message": "rate limit"}),
        json_response(ok_payload([ROW])),
    )
    client, sleeps = make_client(transport, backoff_seconds=2.0)
    assert fetch(client)["candles"] == [ROW]
    assert sleeps == [2.0, 4.0]
    assert len(transport.calls) == 3


def test_rate_limit_exhausts_retries():
    transport = ScriptedTransport(json_response({"s": "error", "code": 429, "message": "slow"}, status=429))
    client, sleeps = make_client(transport, max_retries=2)
    with pytest.raises(FyersRateLimitError):
        fetch(client)
    assert len(transport.calls) == 3 and len(sleeps) == 2


def test_retry_after_header_is_honoured_and_capped():
    transport = ScriptedTransport(
        json_response({}, status=429, headers={"Retry-After": "7"}),
        json_response({}, status=429, headers={"retry-after": "9999"}),
        json_response(ok_payload([ROW])),
    )
    client, sleeps = make_client(transport)
    fetch(client)
    assert sleeps == [7.0, 60.0]


def test_server_errors_and_network_errors_are_retried():
    transport = ScriptedTransport(
        HttpResponse(503, {}, b"<html>busy</html>"),
        FyersNetworkError("network error: TimeoutError"),
        json_response(ok_payload([ROW])),
    )
    client, sleeps = make_client(transport)
    assert fetch(client)["candles"] == [ROW]
    assert len(sleeps) == 2


def test_persistent_network_error_raises_network_error():
    client, _ = make_client(ScriptedTransport(FyersNetworkError("network error: URLError")), max_retries=1)
    with pytest.raises(FyersNetworkError):
        fetch(client)


def test_other_api_errors_are_not_retried():
    transport = ScriptedTransport(json_response({"s": "error", "code": -300, "message": "Please provide a valid symbol"}))
    client, _ = make_client(transport)
    with pytest.raises(FyersApiError, match="valid symbol") as info:
        fetch(client)
    assert not isinstance(info.value, (FyersAuthError, FyersRateLimitError))
    assert info.value.code == -300 and len(transport.calls) == 1


@pytest.mark.parametrize("response", [
    HttpResponse(200, {}, b"<html>not json</html>"),
    json_response([1, 2, 3]),
    HttpResponse(400, {}, b"bad request"),
])
def test_malformed_or_unexpected_responses_raise_api_error(response):
    client, _ = make_client(ScriptedTransport(response))
    with pytest.raises(FyersApiError):
        fetch(client)


def test_errors_and_logs_never_contain_credentials_even_if_api_echoes_them(caplog):
    echo = {"s": "error", "code": -16, "message": f"bad token {FAKE_TOKEN} for {FAKE_APP_ID}"}
    transport = ScriptedTransport(
        json_response({"s": "error", "code": 429, "message": f"limit {FAKE_TOKEN}"}, status=429),
        json_response(echo),
    )
    client, _ = make_client(transport)
    with caplog.at_level(logging.DEBUG), pytest.raises(FyersAuthError) as info:
        fetch(client)
    everything = str(info.value) + caplog.text
    assert FAKE_TOKEN not in everything and FAKE_APP_ID not in everything


def test_raw_http_error_body_is_redacted():
    client, _ = make_client(ScriptedTransport(HttpResponse(400, {}, f"oops {FAKE_TOKEN}".encode())))
    with pytest.raises(FyersApiError) as info:
        fetch(client)
    assert FAKE_TOKEN not in str(info.value)


def test_requests_are_paced():
    now = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    transport = ScriptedTransport(json_response(ok_payload([ROW])))
    client = FyersHistoryClient(fake_credentials(), transport=transport, sleep=sleep,
                                clock=lambda: now[0], min_interval_seconds=1.0)
    fetch(client)
    assert sleeps == []  # first request goes straight out
    now[0] += 0.25
    fetch(client)
    assert sleeps == [pytest.approx(0.75)]
