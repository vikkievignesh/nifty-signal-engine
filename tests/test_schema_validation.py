import pandas as pd
import pytest

from nifty_signal_engine.validation import CandleValidationError, validate_candles

from conftest import make_candles


def test_valid_synthetic_frame_passes(synthetic):
    report = validate_candles(synthetic, require_volume=True)
    assert report.ok, report.errors
    assert report.warnings == []


def test_missing_column():
    df = make_candles("2024-01-02", [100, 101]).drop(columns="low")
    assert "missing columns" in validate_candles(df).errors[0]


def test_require_volume():
    df = make_candles("2024-01-02", [100, 101])
    assert not validate_candles(df, require_volume=True).ok


def test_naive_timestamps_rejected():
    df = make_candles("2024-01-02", [100, 101])
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    assert "timezone-aware" in validate_candles(df).errors[0]


def test_wrong_timezone_rejected():
    df = make_candles("2024-01-02", [100, 101])
    df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    assert "expected Asia/Kolkata" in validate_candles(df).errors[0]


def test_duplicates_and_order():
    df = make_candles("2024-01-02", [100, 101, 102])
    dup = pd.concat([df, df.iloc[[1]]], ignore_index=True)
    errors = " ".join(validate_candles(dup).errors)
    assert "duplicate" in errors and "ascending" in errors


def test_off_grid_and_outside_session():
    df = make_candles("2024-01-02", [100, 101])
    df.loc[1, "timestamp"] = df.loc[1, "timestamp"] + pd.Timedelta(minutes=2)
    assert any("5-minute grid" in e for e in validate_candles(df).errors)

    late = make_candles("2024-01-02", [100, 101], start="15:25")
    report = validate_candles(late)
    assert any("outside" in e for e in report.errors)
    relaxed = validate_candles(late, check_session_hours=False)
    assert relaxed.ok and any("outside" in w for w in relaxed.warnings)


def test_ohlc_consistency():
    df = make_candles("2024-01-02", [100, 101, 102])
    df.loc[0, "high"] = df.loc[0, "low"] - 1
    df.loc[1, "low"] = df.loc[1, "close"] + 0.5
    errors = " ".join(validate_candles(df).errors)
    assert "high < low" in errors
    assert "low above open/close" in errors


def test_non_positive_and_missing_prices():
    df = make_candles("2024-01-02", [100, 101, 102])
    df.loc[0, "open"] = -1.0
    df.loc[1, "close"] = float("nan")
    errors = " ".join(validate_candles(df).errors)
    assert "positive" in errors and "missing OHLC" in errors


def test_negative_volume_error_and_nan_volume_warning():
    df = make_candles("2024-01-02", [100, 101, 102], volume=[10, -1, float("nan")])
    report = validate_candles(df)
    assert any("negative volume" in e for e in report.errors)
    assert any("missing volume" in w for w in report.warnings)


def test_incomplete_session_is_warning_only():
    df = make_candles("2024-01-02", [100, 101])
    report = validate_candles(df)
    assert report.ok
    assert any("sessions without 75" in w for w in report.warnings)


def test_raise_if_errors():
    df = make_candles("2024-01-02", [100]).drop(columns="close")
    with pytest.raises(CandleValidationError):
        validate_candles(df).raise_if_errors()


# --- flat and zero-volume candles are observations (warnings), never errors ---------------------

def _flat_warnings(report):
    return [w for w in report.warnings if "flat" in w]


def _zero_volume_warnings(report):
    return [w for w in report.warnings if "zero volume" in w]


def test_flat_candles_warn_with_count_and_are_not_errors():
    df = make_candles("2024-01-02", [100, 101, 102, 103])
    for i in (1, 3):
        df.loc[i, ["open", "high", "low", "close"]] = 50.0
    before = df.copy()
    report = validate_candles(df)
    assert report.ok, report.errors
    assert any("2 flat candles" in w for w in _flat_warnings(report))
    assert _zero_volume_warnings(report) == []
    pd.testing.assert_frame_equal(df, before, check_exact=True)


def test_zero_volume_candles_warn_with_count_and_are_not_errors():
    df = make_candles("2024-01-02", [100, 101, 102, 103], volume=[10, 0, 5, 0])
    before = df.copy()
    report = validate_candles(df, require_volume=True)
    assert report.ok, report.errors
    assert any("2 candles with zero volume" in w for w in _zero_volume_warnings(report))
    assert _flat_warnings(report) == []
    pd.testing.assert_frame_equal(df, before, check_exact=True)


def test_flat_and_zero_volume_together_report_each_count():
    df = make_candles("2024-01-02", [100, 101, 102], volume=[0, 0, 7])
    df.loc[0, ["open", "high", "low", "close"]] = 50.0
    report = validate_candles(df)
    assert report.ok, report.errors
    assert any("1 flat candle" in w for w in report.warnings)
    assert any("2 candles with zero volume" in w for w in report.warnings)


def test_normal_candles_raise_neither_warning():
    df = make_candles("2024-01-02", [100, 101, 102], volume=[10, 20, 30])
    report = validate_candles(df, require_volume=True)
    assert report.ok
    assert _flat_warnings(report) == [] and _zero_volume_warnings(report) == []


def test_missing_volume_is_not_counted_as_zero_volume():
    df = make_candles("2024-01-02", [100, 101], volume=[float("nan"), 5])
    assert _zero_volume_warnings(validate_candles(df)) == []


def test_flat_candle_with_inconsistent_neighbour_still_errors():
    df = make_candles("2024-01-02", [100, 101])
    df.loc[0, ["open", "high", "low", "close"]] = 50.0
    df.loc[1, "high"] = df.loc[1, "low"] - 1
    report = validate_candles(df)
    assert not report.ok and any("high < low" in e for e in report.errors)
