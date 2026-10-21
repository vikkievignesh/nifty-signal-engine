import pandas as pd
import pytest

from nifty_signal_engine.features import RelativeVolumeMode, UnsupportedVolumeSourceError, relative_volume
from nifty_signal_engine.schema import VolumeSource

from conftest import make_candles


def test_rolling_baseline_excludes_current_candle():
    df = make_candles("2024-01-02", [100] * 5, volume=[10, 20, 30, 1000, 40])
    out = relative_volume(df, source=VolumeSource.INDEX_FUTURES, lookback=3)
    assert out["volume_baseline"].iloc[:3].isna().all()
    assert out["volume_baseline"].iloc[3] == pytest.approx(20.0)  # mean(10, 20, 30), not incl. 1000
    assert out["rvol"].iloc[3] == pytest.approx(50.0)
    assert out["volume_baseline"].iloc[4] == pytest.approx((20 + 30 + 1000) / 3)


def test_time_of_day_baseline_uses_prior_sessions_same_slot():
    days = [make_candles(f"2024-01-0{d}", [100, 100], volume=[v, 5]) for d, v in zip((2, 3, 4), (10, 30, 60))]
    df = pd.concat(days, ignore_index=True)
    out = relative_volume(df, source=VolumeSource.CONSTITUENT_AGGREGATE, lookback=2,
                          mode=RelativeVolumeMode.TIME_OF_DAY)
    # third session 09:15 slot: baseline = mean(10, 30) of prior sessions' 09:15 candles
    assert out["volume_baseline"].iloc[4] == pytest.approx(20.0)
    assert out["rvol"].iloc[4] == pytest.approx(3.0)
    assert out["volume_baseline"].iloc[5] == pytest.approx(5.0)
    assert out["volume_baseline"].iloc[:4].isna().all()


@pytest.mark.parametrize("source", [VolumeSource.NONE, VolumeSource.INDEX_REPORTED])
def test_unsupported_sources_rejected(source):
    df = make_candles("2024-01-02", [100] * 3, volume=[1, 2, 3])
    with pytest.raises(UnsupportedVolumeSourceError):
        relative_volume(df, source=source, lookback=1)


def test_explicit_override_allows_unsupported_source():
    df = make_candles("2024-01-02", [100] * 3, volume=[1, 2, 4])
    out = relative_volume(df, source=VolumeSource.INDEX_REPORTED, lookback=1, allow_unsupported_source=True)
    assert out["rvol"].iloc[2] == pytest.approx(2.0)


def test_zero_baseline_gives_nan():
    df = make_candles("2024-01-02", [100] * 3, volume=[0, 0, 5])
    out = relative_volume(df, source=VolumeSource.ETF_PROXY, lookback=2)
    assert pd.isna(out["rvol"].iloc[2])


def test_missing_volume_column():
    with pytest.raises(ValueError, match="volume"):
        relative_volume(make_candles("2024-01-02", [100]), source=VolumeSource.INDEX_FUTURES)


@pytest.mark.parametrize("mode", list(RelativeVolumeMode))
def test_causal_prefix_invariance(synthetic, mode):
    full = relative_volume(synthetic, source=VolumeSource.INDEX_FUTURES, lookback=3, mode=mode)
    for k in (80, 200, 333):
        part = relative_volume(synthetic.iloc[:k], source=VolumeSource.INDEX_FUTURES, lookback=3, mode=mode)
        pd.testing.assert_frame_equal(part, full.iloc[:k])
