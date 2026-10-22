import pandas as pd

from nifty_signal_engine.data_quality import audit_dataset, compare_datasets


def make_candles(count=5):
    timestamps = pd.date_range(
        "2024-01-02 09:15",
        periods=count,
        freq="5min",
        tz="Asia/Kolkata",
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0 + i for i in range(count)],
            "high": [102.0 + i for i in range(count)],
            "low": [99.0 + i for i in range(count)],
            "close": [101.0 + i for i in range(count)],
            "volume": [1000.0] * count,
        }
    )


def test_audit_reports_basic_dataset_summary():
    df = make_candles()
    report = audit_dataset(df, volume_source="index_futures")

    assert report["rows"] == 5
    assert report["duplicate_timestamps"] == 0
    assert report["first_timestamp"] == str(df["timestamp"].min())
    assert report["last_timestamp"] == str(df["timestamp"].max())
    assert len(report["sessions"]) == 1


def test_audit_detects_duplicate_timestamps_without_mutating_input():
    df = make_candles()
    df.loc[1, "timestamp"] = df.loc[0, "timestamp"]
    original = df.copy(deep=True)

    report = audit_dataset(df)

    assert report["duplicate_timestamps"] == 2
    pd.testing.assert_frame_equal(df, original)


def test_audit_flags_incomplete_session():
    df = make_candles(74)

    report = audit_dataset(df)

    assert len(report["sessions"]) == 1
    assert report["sessions"][0]["rows"] == 74
    assert report["sessions"][0]["missing_expected_candles"] == 1
    assert any("observed sessions" in warning for warning in report["warnings"])


def test_audit_warns_about_spot_index_volume():
    report = audit_dataset(make_candles(), volume_source="index_reported")

    assert any("not validated traded volume" in warning for warning in report["warnings"])


def test_audit_flags_flat_candles_and_zero_volume():
    df = make_candles()
    df.loc[0, ["open", "high", "low", "close"]] = 100.0
    df.loc[0, "volume"] = 0.0

    report = audit_dataset(df)

    assert report["flat_candles"] == 1
    assert report["zero_volume_candles"] == 1


def test_audit_flags_repeated_ohlcv_run_without_deleting_rows():
    df = make_candles(7)
    for i in range(2, 6):
        df.loc[i, ["open", "high", "low", "close", "volume"]] = (
            df.loc[1, ["open", "high", "low", "close", "volume"]].to_numpy()
        )
    original = df.copy(deep=True)

    report = audit_dataset(df)

    assert report["repeated_ohlcv_transitions"] == 4
    pd.testing.assert_frame_equal(df, original)


def test_audit_checks_metadata_counts():
    df = make_candles()
    report = audit_dataset(
        df,
        metadata={"rows": 5, "sessions": 1, "volume_source": "index_futures"},
        volume_source="index_futures",
    )

    assert report["metadata_checks"]["metadata_rows_match"] is True
    assert report["metadata_checks"]["metadata_sessions_match"] is True


def test_compare_datasets_reports_matching_overlap():
    left = make_candles(5)
    right = left.iloc[2:].copy()

    report = compare_datasets(left, right)

    assert report["overlapping_timestamps"] == 3
    assert report["candles_with_any_difference"] == 0
    assert report["ohlcv_matches"]["open"] == 3
    assert report["ohlcv_matches"]["volume"] == 3


def test_compare_datasets_detects_value_mismatch():
    left = make_candles(5)
    right = left.iloc[2:].copy().reset_index(drop=True)
    right.loc[0, "close"] += 1

    report = compare_datasets(left, right)

    assert report["candles_with_any_difference"] == 1
    assert report["ohlcv_matches"]["close"] == 2


def test_compare_datasets_rejects_duplicate_timestamps():
    left = make_candles()
    left.loc[1, "timestamp"] = left.loc[0, "timestamp"]

    try:
        compare_datasets(left, make_candles())
    except ValueError as exc:
        assert "duplicate timestamps" in str(exc)
    else:
        raise AssertionError("Expected duplicate timestamps to be rejected")


def test_repeated_ohlc_is_detected_when_volume_changes():
    df = make_candles(7)

    # Five consecutive candles share OHLC, but volume changes each time.
    for i in range(1, 6):
        df.loc[i, ["open", "high", "low", "close"]] = (
            df.loc[0, ["open", "high", "low", "close"]].to_numpy()
        )
        df.loc[i, "volume"] = 1000.0 + i

    report = audit_dataset(df)

    assert report["repeated_ohlc_transitions"] == 5
    assert report["repeated_ohlcv_transitions"] == 0
    assert any("repeated OHLC transitions" in w for w in report["warnings"])
