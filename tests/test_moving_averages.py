import math

import pandas as pd
import pytest

from nifty_signal_engine.features import ema, sma


def test_sma_values_and_warmup():
    out = sma(pd.Series([1.0, 2, 3, 4, 5]), 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_matches_recursion():
    values = [10.0, 11, 12, 11, 13, 14]
    span = 3
    alpha = 2 / (span + 1)
    expected = [values[0]]
    for v in values[1:]:
        expected.append(alpha * v + (1 - alpha) * expected[-1])
    out = ema(pd.Series(values), span)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == pytest.approx(expected[2:])


@pytest.mark.parametrize("bad", [0, -1, 2.5, True])
def test_invalid_periods(bad):
    with pytest.raises(ValueError):
        sma(pd.Series([1.0]), bad)
    with pytest.raises(ValueError):
        ema(pd.Series([1.0]), bad)


def test_causal_prefix_invariance():
    s = pd.Series([float(x) for x in range(1, 40)])
    full_e, full_s = ema(s, 5), sma(s, 5)
    for k in (5, 17, 30):
        pd.testing.assert_series_equal(ema(s.iloc[:k], 5), full_e.iloc[:k])
        pd.testing.assert_series_equal(sma(s.iloc[:k], 5), full_s.iloc[:k])
    assert not math.isnan(full_e.iloc[-1])
