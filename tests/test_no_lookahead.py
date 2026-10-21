"""End-to-end look-ahead checks: results for candles <= t must not change when
later candles are appended."""

import pandas as pd
import pytest

from nifty_signal_engine.config import Settings
from nifty_signal_engine.pipeline import build_example_machine, build_feature_frame
from nifty_signal_engine.schema import VolumeSource
from nifty_signal_engine.signals import run_signals
from nifty_signal_engine.synthetic import generate_synthetic_candles

SETTINGS = Settings(volume_source=VolumeSource.INDEX_FUTURES, rvol_lookback=5)


@pytest.fixture(scope="module")
def candles():
    return generate_synthetic_candles(n_sessions=8, seed=3)


@pytest.mark.parametrize("cut", [75, 76, 150, 260, 413, 599])
def test_feature_frame_prefix_invariance(candles, cut):
    full = build_feature_frame(candles, SETTINGS)
    part = build_feature_frame(candles.iloc[:cut], SETTINGS)
    pd.testing.assert_frame_equal(part, full.iloc[:cut])


def test_future_shock_does_not_change_past_features(candles):
    full = build_feature_frame(candles, SETTINGS)
    shocked = candles.copy()
    shocked.loc[400:, ["open", "high", "low", "close"]] *= 1.5
    shocked.loc[400:, "volume"] *= 10
    after = build_feature_frame(shocked, SETTINGS)
    pd.testing.assert_frame_equal(after.iloc[:400], full.iloc[:400])


def test_signal_prefix_invariance(candles):
    full_frame = build_feature_frame(candles, SETTINGS)
    full = run_signals(full_frame, build_example_machine(SETTINGS))
    for cut in (150, 300, 450):
        part = run_signals(full_frame.iloc[:cut], build_example_machine(SETTINGS))
        last_ts = full_frame["timestamp"].iloc[cut - 1]
        assert part.transitions == [t for t in full.transitions if t.timestamp <= last_ts]
        assert part.alerts == [a for a in full.alerts if a.candle_timestamp <= last_ts]


def test_rvol_disabled_for_unsupported_source(candles):
    frame = build_feature_frame(candles, Settings(volume_source=VolumeSource.INDEX_REPORTED))
    assert frame["rvol"].isna().all()


def test_first_session_has_no_cpr_and_unknown_regime(candles):
    frame = build_feature_frame(candles, SETTINGS)
    assert frame["cpr_pivot"].iloc[:75].isna().all()
    assert (frame["regime"].iloc[:75] == "UNKNOWN").all()
    assert frame["cpr_pivot"].iloc[75:].notna().all()
