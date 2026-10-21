import hashlib
import json
import logging
import urllib.parse

import pytest

from nifty_signal_engine.fyers import AuthCredentials, FyersAuthError, HttpResponse, SecretStr, exchange_auth_code
from nifty_signal_engine.fyers.auth import describe_exchange_request, summarise_response
from nifty_signal_engine.fyers.login import main

from fyers_auth_helpers import (
    ACCESS_TOKEN,
    APP_ID,
    APP_ID_HASH,
    APP_SECRET,
    AUTH_CODE,
    ScriptedPost,
    creds,
    json_resp,
)

COOKIE = "session=cookievalue-0123456789abcdef-SECRET"


def resp(status=403, body="", content_type="application/json", headers=None):
    data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
    return HttpResponse(status, {"Content-Type": content_type, **(headers or {})}, data)


def exchange_error(response, credentials=None):
    with pytest.raises(FyersAuthError) as info:
        exchange_auth_code(credentials or creds(), SecretStr(AUTH_CODE), transport=ScriptedPost(response))
    return info.value


def assert_clean(text):
    for secret in (APP_SECRET, APP_ID_HASH, APP_ID_HASH.upper(), AUTH_CODE, ACCESS_TOKEN, COOKIE, "cookievalue"):
        assert secret not in text


# --- what is exposed: allowlisted code/message, otherwise only the shape ------------------------

def test_api_code_and_message_are_exposed():
    error = exchange_error(resp(body={"s": "error", "code": -5, "message": "Invalid app id hash"}))
    assert error.status == 403 and error.code == -5
    text = str(error)
    assert "HTTP 403" in text and "API code -5" in text and 'message "Invalid app id hash"' in text
    assert "[application/json," in text


@pytest.mark.parametrize("body, code, message", [
    ({"code": "-371", "message": "Please provide sha256 hash"}, -371, "Please provide sha256 hash"),   # string code
    ({"error_description": "client not allowed"}, None, "client not allowed"),
    ({"error": "Forbidden"}, None, "Forbidden"),
    ({"detail": "Access denied"}, None, "Access denied"),
    ({"code": True, "message": "x"}, None, "x"),                                                       # bool is not a code
])
def test_fallback_keys_and_code_parsing(body, code, message):
    error = exchange_error(resp(body=body))
    assert error.code == code and f'message "{message}"' in str(error)


def test_json_without_code_or_message_reports_only_its_shape():
    error = exchange_error(resp(body={"foo": "bar", "trace": "internal-trace-id-0123456789abcdef"}))
    text = str(error)
    assert "a JSON object with no API code or message" in text
    assert "foo" not in text and "internal-trace" not in text and error.code is None


@pytest.mark.parametrize("response, shape_text, leaked_fragments", [
    (resp(body="<html><head><title>Attention Required</title></head><body>cf-ray 8a1b2c</body></html>",
          content_type="text/html; charset=UTF-8"), "an HTML page, not a JSON API error", ["Attention", "cf-ray"]),
    (resp(body="<!doctype html><p>blocked", content_type="application/octet-stream"),
     "an HTML page, not a JSON API error", ["blocked"]),                                    # sniffed, wrong header
    (resp(body="Forbidden by policy ticket 4711", content_type="text/plain"),
     "a plain-text body (content not shown)", ["policy", "4711"]),
    (resp(body="", content_type="application/json"), "an empty body", []),
    (resp(body="   \n", content_type="text/plain"), "an empty body", []),
    (resp(body=json.dumps([1, 2, 3])), "JSON that is not an object", ["1, 2"]),
    (resp(body=bytes(range(128, 200)) * 30, content_type="application/octet-stream"),
     "unrecognised format", []),
])
def test_non_api_bodies_are_described_by_shape_never_by_content(response, shape_text, leaked_fragments):
    error = exchange_error(response)
    text = str(error)
    assert shape_text in text and "HTTP 403" in text
    for fragment in leaked_fragments:
        assert fragment not in text
    assert error.code is None


def test_size_is_a_number_not_content():
    text = str(exchange_error(resp(body="<html>" + "x" * 4000 + "</html>", content_type="text/html")))
    assert "text/html, 4013 bytes" in text and "xxxx" not in text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_every_failing_status_is_summarised_the_same_way(status):
    error = exchange_error(resp(status=status, body={"s": "error", "code": -413, "message": "Invalid auth code"}))
    assert f"HTTP {status}" in str(error) and "API code -413" in str(error) and error.status == status


def test_http_200_with_error_status_in_body_is_also_summarised():
    error = exchange_error(resp(status=200, body={"s": "error", "code": -5, "message": "invalid app id hash"}))
    assert "HTTP 200" in str(error) and "API code -5" in str(error)


# --- nothing else is ever exposed --------------------------------------------------------------

def test_only_allowlisted_fields_are_read_other_fields_and_headers_never_appear():
    body = {"s": "error", "code": -5, "message": "Invalid app id hash",
            "access_token": ACCESS_TOKEN, "refresh_token": ACCESS_TOKEN[::-1], "appIdHash": APP_ID_HASH,
            "request": {"code": AUTH_CODE, "grant_type": "authorization_code"}, "cookie": COOKIE,
            "debug": f"echo {APP_SECRET}"}
    error = exchange_error(resp(body=body, headers={"Set-Cookie": COOKIE, "Authorization": f"Bearer {ACCESS_TOKEN}",
                                                    "X-Request-Id": "req-123"}))
    text = str(error)
    assert_clean(text)
    for leaked in ("refresh_token", "grant_type", "debug", "req-123", "Set-Cookie", "Bearer"):
        assert leaked not in text
    assert "API code -5" in text


@pytest.mark.parametrize("echo", [
    APP_SECRET,
    APP_ID_HASH,
    APP_ID_HASH.upper(),                                  # echoed in another case
    urllib.parse.quote(APP_SECRET + "/x+y", safe=""),     # percent-encoded
    AUTH_CODE,
    AUTH_CODE.lower(),
    APP_ID,
    "ey" + "A1b2C3d4E5" * 5,                              # an unrelated long opaque token / JWT-like blob
    hashlib.sha256(b"something else entirely").hexdigest(),   # any 64-hex string
])
def test_secrets_echoed_in_a_message_are_scrubbed_in_every_form(echo):
    error = exchange_error(resp(body={"code": -5, "message": f"bad value {echo} in request"}))
    text = str(error)
    assert echo not in text and echo.lower() not in text.lower()
    assert "***" in text and "bad value" in text
    assert_clean(text)


def test_message_is_sanitised_and_bounded():
    nasty = "bad\x1b[31m<script>alert(1)</script>\r\nSet-Cookie: x=y\n" + "A " * 400
    text = str(exchange_error(resp(body={"message": nasty})))
    assert "\x1b" not in text and "<" not in text and "\n" not in text and "\r" not in text
    message = text.split('message "', 1)[1].split('"', 1)[0]
    assert 0 < len(message) <= 120


def test_nothing_is_logged_during_a_rejected_exchange(caplog):
    with caplog.at_level(logging.DEBUG):
        exchange_error(resp(body={"code": -5, "message": f"echo {APP_SECRET} {APP_ID_HASH} {AUTH_CODE}"}))
    assert_clean(caplog.text)


def test_redact_handles_case_and_url_encoding_directly():
    c = creds()
    text = f"{APP_SECRET.lower()} {urllib.parse.quote_plus(APP_SECRET)} {APP_ID_HASH.upper()} {APP_ID.lower()}"
    cleaned = c.redact(text, AUTH_CODE)
    assert APP_SECRET.lower() not in cleaned.lower() and APP_ID_HASH not in cleaned.upper()
    assert c.redact(f"x {AUTH_CODE} y", AUTH_CODE) == "x *** y"


def test_summary_is_safe_to_repr():
    summary = summarise_response(resp(body={"code": -5, "message": "Invalid"}), lambda t: t)
    assert "Invalid" in summary.describe() and ACCESS_TOKEN not in repr(summary)


# --- request shape and App ID suffix ----------------------------------------------------------

def test_request_uses_exactly_the_documented_fields_and_the_full_app_id():
    transport = ScriptedPost(json_resp({"s": "ok", "access_token": ACCESS_TOKEN}))
    exchange_auth_code(creds(), SecretStr(AUTH_CODE), transport=transport)
    url, headers, body = transport.calls[0]
    assert url == "https://api-t1.fyers.in/api/v3/validate-authcode"
    assert set(body) == {"grant_type", "appIdHash", "code"} and body["grant_type"] == "authorization_code"
    assert body["appIdHash"] == APP_ID_HASH  # literal SHA256("TESTAPP123-100:<secret>"): suffix INCLUDED
    stripped = hashlib.sha256(f"{APP_ID.split('-')[0]}:{APP_SECRET}".encode()).hexdigest()
    assert stripped != APP_ID_HASH  # dropping the suffix would change the hash, so it must be kept


def test_rejection_flags_an_app_id_without_the_dashboard_suffix_but_not_a_correct_one():
    no_suffix = AuthCredentials("TESTAPP123", APP_SECRET)
    flagged = str(exchange_error(resp(body={"code": -5, "message": "x"}), no_suffix))
    assert "does not look like the dashboard format" in flagged and "TESTAPP123" not in flagged
    fine = str(exchange_error(resp(body={"code": -5, "message": "x"})))
    assert "does not look like" not in fine and APP_ID not in fine


def test_rejection_message_gives_the_actionable_next_step_without_claiming_a_cause():
    text = str(exchange_error(resp(body="<html>x</html>", content_type="text/html")))
    assert "fresh auth code" in text and "FYERS_APP_ID" in text
    for claim in ("because", "caused by", "root cause"):
        assert claim not in text


# --- offline --check ----------------------------------------------------------------------------

def run_check(tmp_path, env, *argv):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    calls = []
    code = main(["--env-file", str(env_file), "--check", *argv], env=env, open_url=calls.append,
                post_transport=lambda *a: calls.append("net"), server_factory=lambda port: calls.append("server"))
    return code, calls


def test_check_describes_the_request_and_prints_no_secret(tmp_path, capsys):
    code, calls = run_check(tmp_path, {"FYERS_APP_ID": APP_ID, "FYERS_APP_SECRET": APP_SECRET})
    out = capsys.readouterr().out
    assert code == 0 and calls == []  # no server, no browser, no network
    assert "POST https://api-t1.fyers.in/api/v3/validate-authcode" in out
    assert 'grant_type="authorization_code"' in out and "appIdHash=<64 hex chars>" in out
    assert "<App ID>:<App Secret>" in out and "matches the dashboard format <ID>-<digits> : yes" in out
    assert "http://127.0.0.1:5000/callback" in out
    assert_clean(out) and APP_ID not in out


def test_check_reflects_the_configured_port(tmp_path, capsys):
    run_check(tmp_path, {"FYERS_APP_ID": APP_ID, "FYERS_APP_SECRET": APP_SECRET}, "--port", "5001")
    assert "Redirect URI the login URL will carry         : http://127.0.0.1:5001/callback" in capsys.readouterr().out


@pytest.mark.parametrize("app_id, secret, expect", [
    ("TESTAPP123", APP_SECRET, "NO   <- a missing '-100'-style suffix changes the hash"),
    ('"TESTAPP123-100"', APP_SECRET, "wrapped in quote characters                 : YES"),
    ("TESTAPP123-100", f"'{APP_SECRET}'", "wrapped in quote characters                 : YES"),
    ("TEST:APP-100", APP_SECRET, "has characters other than letters/digits/-/_: YES"),
])
def test_check_flags_common_credential_mistakes_without_revealing_them(tmp_path, capsys, app_id, secret, expect):
    code, calls = run_check(tmp_path, {"FYERS_APP_ID": app_id, "FYERS_APP_SECRET": secret})
    out = capsys.readouterr().out
    assert code == 0 and expect in out and calls == []
    assert APP_SECRET not in out and "TESTAPP123" not in out


def test_check_requires_credentials_and_still_makes_no_network_call(tmp_path, capsys):
    code, calls = run_check(tmp_path, {"FYERS_APP_ID": APP_ID})
    assert code == 1 and "FYERS_APP_SECRET" in capsys.readouterr().err and calls == []


def test_describe_exchange_request_is_pure_text():
    lines = describe_exchange_request(creds(), 5000)
    assert all(isinstance(line, str) for line in lines)
    assert_clean("\n".join(lines))


@pytest.mark.parametrize("field", ["debug", "request", "cookie", "access_token", "token", "hash", "data", "result",
                                   "reason", "errors", "msg", "info", "trace", "details", "body", "response",
                                   "appIdHash", "refresh_token", "stack", "title", "text", "description"])
def test_a_field_outside_the_allowlist_is_never_shown_even_when_it_is_the_only_one(field):
    """No fallback to 'whatever string is in the body': only the allowlisted keys may be read."""
    leaked = f"internal-{field}-VALUE-0123456789"
    error = exchange_error(resp(body={field: leaked}))
    text = str(error)
    assert leaked not in text and "VALUE" not in text
    assert "a JSON object with no API code or message" in text and error.code is None
