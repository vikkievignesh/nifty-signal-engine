"""Preliminary, rule-based market-regime classifier.

UNVALIDATED: the rules and default thresholds below are placeholders chosen to be
easy to reason about, not research results. They have not been tested for
predictive value. Treat the output as a descriptive label only.

Inputs per candle (all causal): ``close``, ``ema_fast``, ``ema_slow`` and,
when ``use_cpr_filter`` is on, ``cpr_top``/``cpr_bottom`` from the previous session.

    slope_pct  = % change of ema_slow over ``slope_lookback`` candles
    spread_pct = (ema_fast - ema_slow) / close * 100

    UNKNOWN     any required input is NaN (warm-up, missing CPR)
    TREND_UP    close > ema_fast > ema_slow, slope_pct >= trend_slope_min_pct
                [and close > cpr_top]
    TREND_DOWN  close < ema_fast < ema_slow, slope_pct <= -trend_slope_min_pct
                [and close < cpr_bottom]
    RANGE       |spread_pct| <= range_spread_max_pct and |slope_pct| < trend_slope_min_pct
    MIXED       anything else
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RegimeConfig:
    """Thresholds for :func:`classify_regime`. All defaults are UNVALIDATED."""

    slope_lookback: int = 6
    trend_slope_min_pct: float = 0.02
    range_spread_max_pct: float = 0.05
    use_cpr_filter: bool = True

    def __post_init__(self) -> None:
        if self.slope_lookback < 1:
            raise ValueError("slope_lookback must be >= 1")
        for name in ("trend_slope_min_pct", "range_spread_max_pct"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative number, got {value!r}")


def classify_regime(features: pd.DataFrame, config: RegimeConfig = RegimeConfig()) -> pd.Series:
    """Label each candle with a :class:`MarketRegime` value (string)."""
    close = features["close"]
    fast = features["ema_fast"]
    slow = features["ema_slow"]
    prior_slow = slow.shift(config.slope_lookback)
    slope_pct = (slow - prior_slow) / prior_slow * 100.0
    spread_pct = (fast - slow) / close * 100.0

    inputs = [close, fast, slow, slope_pct]
    up = (close > fast) & (fast > slow) & (slope_pct >= config.trend_slope_min_pct)
    down = (close < fast) & (fast < slow) & (slope_pct <= -config.trend_slope_min_pct)
    if config.use_cpr_filter:
        top, bottom = features["cpr_top"], features["cpr_bottom"]
        inputs += [top, bottom]
        up &= close > top
        down &= close < bottom
    rng = (spread_pct.abs() <= config.range_spread_max_pct) & (slope_pct.abs() < config.trend_slope_min_pct)
    unknown = pd.concat(inputs, axis=1).isna().any(axis=1)

    labels = np.select(
        [unknown, up, down, rng],
        [MarketRegime.UNKNOWN.value, MarketRegime.TREND_UP.value,
         MarketRegime.TREND_DOWN.value, MarketRegime.RANGE.value],
        default=MarketRegime.MIXED.value,
    )
    return pd.Series(labels, index=features.index, name="regime", dtype=object)
