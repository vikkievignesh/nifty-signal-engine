"""Simple and exponential moving averages (causal, continuous across sessions).

Averages run over the continuous 5-minute series, so the first candles of a
session include the previous session's candles. Warm-up rows are NaN.
"""

from __future__ import annotations

import pandas as pd


def _check_period(period: int, name: str) -> None:
    if not isinstance(period, int) or isinstance(period, bool) or period < 1:
        raise ValueError(f"{name} must be a positive integer, got {period!r}")


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over the last ``window`` values including the current."""
    _check_period(window, "window")
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Recursive EMA with alpha = 2 / (span + 1), seeded at the first value.

    The first ``span - 1`` values are NaN so the seed's influence has decayed
    somewhat before the EMA is used.
    """
    _check_period(span, "span")
    return series.ewm(span=span, adjust=False, min_periods=span).mean()
