from nifty_signal_engine.__main__ import main
from nifty_signal_engine.synthetic import generate_synthetic_candles
from nifty_signal_engine.validation import validate_candles

from conftest import SAMPLE_CSV


def test_synthetic_is_valid_and_deterministic():
    a = generate_synthetic_candles(n_sessions=3, seed=1)
    b = generate_synthetic_candles(n_sessions=3, seed=1)
    assert a.equals(b)
    assert len(a) == 225
    assert validate_candles(a, require_volume=True).ok


def test_cli_runs_on_sample(capsys, monkeypatch):
    monkeypatch.setenv("NIFTY_VOLUME_SOURCE", "index_futures")
    assert main(["--csv", str(SAMPLE_CSV)]) == 0
    out = capsys.readouterr().out
    assert "UNVALIDATED" in out and "candles: 750" in out
