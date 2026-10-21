import logging
import os
import stat

import pytest

from nifty_signal_engine.fyers import (
    Credentials,
    CredentialsError,
    MissingCredentialsError,
    load_credentials,
    parse_dotenv,
)

from fyers_helpers import FAKE_APP_ID, FAKE_TOKEN


def test_loads_from_environment(tmp_path):
    creds = load_credentials({"FYERS_APP_ID": FAKE_APP_ID, "FYERS_ACCESS_TOKEN": FAKE_TOKEN}, tmp_path / ".env")
    assert creds.authorization == f"{FAKE_APP_ID}:{FAKE_TOKEN}"


def test_loads_from_dotenv_file(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"# comment\nFYERS_APP_ID={FAKE_APP_ID}\nexport FYERS_ACCESS_TOKEN=\"{FAKE_TOKEN}\"\n")
    dotenv.chmod(0o600)
    assert load_credentials({}, dotenv).access_token == FAKE_TOKEN


def test_environment_overrides_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FYERS_APP_ID=from-file\nFYERS_ACCESS_TOKEN=file-token\n")
    creds = load_credentials({"FYERS_ACCESS_TOKEN": FAKE_TOKEN}, dotenv)
    assert (creds.app_id, creds.access_token) == ("from-file", FAKE_TOKEN)


def test_dotenv_is_not_copied_into_os_environ(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"FYERS_APP_ID={FAKE_APP_ID}\nFYERS_ACCESS_TOKEN={FAKE_TOKEN}\n")
    load_credentials({}, dotenv)
    assert "FYERS_ACCESS_TOKEN" not in os.environ


@pytest.mark.parametrize("env", [{}, {"FYERS_APP_ID": "x"}, {"FYERS_ACCESS_TOKEN": FAKE_TOKEN}, {"FYERS_APP_ID": "  "}])
def test_missing_credentials_names_variables_but_never_values(env, tmp_path):
    with pytest.raises(MissingCredentialsError) as info:
        load_credentials(env, tmp_path / "absent.env")
    assert "FYERS_" in str(info.value)
    assert FAKE_TOKEN not in str(info.value)


def test_repr_and_str_are_masked():
    creds = Credentials(FAKE_APP_ID, FAKE_TOKEN)
    for text in (repr(creds), str(creds), f"{creds}", repr([creds])):
        assert FAKE_TOKEN not in text and FAKE_APP_ID not in text


def test_redact_scrubs_token_and_app_id():
    creds = Credentials(FAKE_APP_ID, FAKE_TOKEN)
    cleaned = creds.redact(f"bad token {FAKE_TOKEN} for app {FAKE_APP_ID}")
    assert FAKE_TOKEN not in cleaned and FAKE_APP_ID not in cleaned and "***" in cleaned


@pytest.mark.parametrize("app_id, token", [("", "t"), ("a", ""), ("a b", "t"), ("a", "t\nX-Evil: 1"), ("a", "t\r")])
def test_rejects_empty_or_header_injecting_values(app_id, token):
    with pytest.raises(CredentialsError):
        Credentials(app_id, token)


def test_parse_dotenv_variants():
    parsed = parse_dotenv("A=1\n  B = two  \nC='q # not comment'\nD=val # inline comment\nexport E=5\n\n# x\nF=\n")
    assert parsed == {"A": "1", "B": "two", "C": "q # not comment", "D": "val", "E": "5", "F": ""}


@pytest.mark.parametrize("text", ["LEAKYWORDS no equals sign\n", "9LEAKYBAD=1\n", "A='LEAKYVALUE\n"])
def test_parse_dotenv_errors_report_line_number_not_content(text):
    with pytest.raises(CredentialsError) as info:
        parse_dotenv("OK=1\n" + text)
    assert "line 2" in str(info.value)
    assert "LEAKY" not in str(info.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_warns_when_dotenv_is_readable_by_others(tmp_path, caplog):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"FYERS_APP_ID={FAKE_APP_ID}\nFYERS_ACCESS_TOKEN={FAKE_TOKEN}\n")
    dotenv.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        load_credentials({}, dotenv)
    assert "chmod 600" in caplog.text
    assert FAKE_TOKEN not in caplog.text

    caplog.clear()
    dotenv.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with caplog.at_level(logging.WARNING):
        load_credentials({}, dotenv)
    assert "chmod" not in caplog.text
