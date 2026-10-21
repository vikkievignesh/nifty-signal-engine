import math

import pandas as pd
import pytest

from nifty_signal_engine.regime import MarketRegime, RegimeConfig, classify_regime

NAN = math.nan
CFG = RegimeConfig(slope_lookback=1, trend_slope_min_pct=0.02, range_spread_max_pct=0.05, use_cpr_filter=True)


def frame(rows):
    """rows: (close, ema_fast, ema_slow, cpr_top, cpr_bottom); first row seeds the slope."""
    return pd.DataFrame(rows, columns=["close", "ema_fast", "ema_slow", "cpr_top", "cpr_bottom"])


def label(rows, cfg=CFG):
    return classify_regime(frame(rows), cfg).iloc[-1]


def test_trend_up():
    assert label([(0, 0, 100.0, 0, 0), (102, 101, 100.1, 99, 98)]) == MarketRegime.TREND_UP


def test_trend_down():
    assert label([(0, 0, 100.0, 0, 0), (98, 99, 99.9, 102, 101)]) == MarketRegime.TREND_DOWN


def test_cpr_filter_blocks_trend():
    rows = [(0, 0, 100.0, 0, 0), (102, 101, 100.1, 105, 104)]  # close below CPR top
    assert label(rows) == MarketRegime.MIXED
    assert label(rows, RegimeConfig(slope_lookback=1, use_cpr_filter=False)) == MarketRegime.TREND_UP


def test_range():
    assert label([(0, 0, 100.0, 0, 0), (100.0, 100.01, 100.005, 101, 99)]) == MarketRegime.RANGE


def test_mixed():
    # strong slope but price below fast EMA
    assert label([(0, 0, 100.0, 0, 0), (100.5, 101, 100.1, 99, 98)]) == MarketRegime.MIXED


def test_unknown_on_missing_inputs():
    out = classify_regime(frame([(100, 100, 100, 99, 98), (101, 100, 100, NAN, NAN)]), CFG)
    assert out.tolist() == [MarketRegime.UNKNOWN, MarketRegime.UNKNOWN]  # warm-up slope, missing CPR


def test_invalid_config():
    with pytest.raises(ValueError):
        RegimeConfig(slope_lookback=0)
