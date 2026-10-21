import pandas as pd
import pytest

from nifty_signal_engine.data_loader import load_candles_csv
from nifty_signal_engine.schema import IST, SCHEMA_COLUMNS
from nifty_signal_engine.validation import CandleValidationError

from conftest import SAMPLE_CSV

ROWS = "{ts1},100,101,99,100.5,10\n{ts2},100.5,102,100,101.5,20\n"


def write(tmp_path, ts1, ts2, header="timestamp,open,high,low,close,volume"):
    path = tmp_path / "c.csv"
    path.write_text(header + "\n" + ROWS.format(ts1=ts1, ts2=ts2))
    return path


def test_load_sample_csv():
    df = load_candles_csv(SAMPLE_CSV, require_volume=True)
    assert list(df.columns) == list(SCHEMA_COLUMNS)
    assert str(df["timestamp"].dt.tz) == "Asia/Kolkata"
    assert len(df) == 750
    assert (df[["open", "high", "low", "close", "volume"]].dtypes == "float64").all()


def test_naive_timestamps_are_ist(tmp_path):
    df = load_candles_csv(write(tmp_path, "2024-01-02 09:15:00", "2024-01-02 09:20:00"))
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-01-02 09:15", tz=IST)


def test_utc_timestamps_converted_to_ist(tmp_path):
    df = load_candles_csv(write(tmp_path, "2024-01-02T03:45:00Z", "2024-01-02T03:50:00+00:00"))
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-01-02 09:15", tz=IST)
    assert str(df["timestamp"].dt.tz) == "Asia/Kolkata"


def test_mixed_offsets_rejected(tmp_path):
    with pytest.raises(CandleValidationError, match="mixes"):
        load_candles_csv(write(tmp_path, "2024-01-02T09:15:00+05:30", "2024-01-02 09:20:00"))


def test_case_insensitive_headers_and_extra_columns(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("Timestamp,Open,High,Low,Close,OI\n2024-01-02 09:15,100,101,99,100,5\n")
    df = load_candles_csv(path)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close"]


def test_invalid_rows_raise(tmp_path):
    with pytest.raises(CandleValidationError, match="ascending"):
        load_candles_csv(write(tmp_path, "2024-01-02 09:20:00", "2024-01-02 09:15:00"))


def test_missing_required_column(tmp_path):
    with pytest.raises(CandleValidationError, match="missing columns"):
        load_candles_csv(write(tmp_path, "2024-01-02 09:15", "2024-01-02 09:20",
                               header="timestamp,open,high,low,price,volume"))
