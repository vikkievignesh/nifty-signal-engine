from datetime import date

import pytest

from nifty_signal_engine.fyers.__main__ import main
from nifty_signal_engine.synthetic import generate_synthetic_candles

from fyers_helpers import FAKE_APP_ID, FAKE_TOKEN, NOW, FakeFyersApi, ScriptedTransport, json_response


def env_for(tmp_path, **extra):
    return {"NIFTY_DATA_DIR": str(tmp_path / "data"), "FYERS_APP_ID": FAKE_APP_ID,
            "FYERS_ACCESS_TOKEN": FAKE_TOKEN, **extra}


def run_cli(tmp_path, argv, transport, env=None):
    return main(["--min-interval", "0", *argv], env=env or env_for(tmp_path), transport=transport,
                sleep=lambda s: None, now=NOW, dotenv_path=tmp_path / "absent.env")


def test_cli_downloads_to_default_ignored_directory(tmp_path, capsys):
    api = FakeFyersApi(generate_synthetic_candles(5, start=date(2024, 1, 1), seed=2))
    assert run_cli(tmp_path, ["--start", "2024-01-01", "--end", "2024-01-05"], api) == 0
    out = capsys.readouterr()
    assert "wrote 375 candles" in out.out
    assert "must not be treated as traded volume" in out.out
    assert (tmp_path / "data" / "local" / "fyers" / "NSE_NIFTY50-INDEX_5m_2024-01-01_2024-01-05.csv").is_file()
    assert FAKE_TOKEN not in out.out + out.err and FAKE_APP_ID not in out.out + out.err


def test_cli_reads_credentials_from_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"FYERS_APP_ID={FAKE_APP_ID}\nFYERS_ACCESS_TOKEN={FAKE_TOKEN}\n")
    dotenv.chmod(0o600)
    api = FakeFyersApi(generate_synthetic_candles(1, start=date(2024, 1, 1), seed=2))
    code = main(["--start", "2024-01-01", "--end", "2024-01-01", "--min-interval", "0"],
                env={"NIFTY_DATA_DIR": str(tmp_path / "d")}, transport=api, sleep=lambda s: None,
                now=NOW, dotenv_path=dotenv)
    assert code == 0


def test_cli_missing_credentials_fails_cleanly(tmp_path, capsys):
    transport = ScriptedTransport(json_response({}))
    code = run_cli(tmp_path, ["--start", "2024-01-01"], transport, env={"NIFTY_DATA_DIR": str(tmp_path)})
    err = capsys.readouterr().err
    assert code == 1 and "FYERS_APP_ID" in err and "FYERS_ACCESS_TOKEN" in err
    assert transport.calls == []  # no request without credentials


def test_cli_auth_failure_reports_without_secrets(tmp_path, capsys):
    transport = ScriptedTransport(json_response({"s": "error", "code": -16, "message": f"bad {FAKE_TOKEN}"}))
    code = run_cli(tmp_path, ["--start", "2024-01-01", "--end", "2024-01-02"], transport)
    out = capsys.readouterr()
    assert code == 1 and "Regenerate the access token" in out.err
    assert FAKE_TOKEN not in out.out + out.err and FAKE_APP_ID not in out.out + out.err


def test_cli_rejects_bad_dates_and_window_sizes(tmp_path, capsys):
    transport = ScriptedTransport(json_response({}))
    with pytest.raises(SystemExit) as exit_info:
        run_cli(tmp_path, ["--start", "01/02/2024"], transport)
    assert exit_info.value.code == 2
    assert run_cli(tmp_path, ["--start", "2024-01-01", "--window-days", "101"], transport) == 1
    assert run_cli(tmp_path, ["--start", "2024-03-01", "--end", "2024-01-01"], transport) == 1
    assert transport.calls == []
