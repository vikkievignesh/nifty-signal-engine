"""Assemble the causal feature frame used by the regime classifier and state machine."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import Settings
from .features import compute_cpr, ema, relative_volume, sma
from .regime import classify_regime
from .schema import CLOSE_COL, TRADED_VOLUME_SOURCES, VOLUME_COL
from .signals.example_rules import example_ruleset
from .signals.state_machine import SignalStateMachine

logger = logging.getLogger(__name__)


def build_feature_frame(candles: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Candles plus CPR, EMA/SMA, RVOL and regime. Each row uses only data up to its close.

    RVOL columns are NaN when the configured volume source is not traded volume.
    """
    frame = candles.copy()
    frame = frame.join(compute_cpr(candles, min_session_candles=settings.cpr_min_session_candles))
    close = candles[CLOSE_COL]
    frame["ema_fast"] = ema(close, settings.ema_fast)
    frame["ema_slow"] = ema(close, settings.ema_slow)
    frame["sma"] = sma(close, settings.sma_period)

    if settings.volume_source in TRADED_VOLUME_SOURCES and VOLUME_COL in candles.columns:
        frame = frame.join(relative_volume(candles, source=settings.volume_source,
                                           lookback=settings.rvol_lookback, mode=settings.rvol_mode))
    else:
        logger.info("RVOL disabled: volume source %r is not a supported traded-volume source",
                    settings.volume_source.value)
        frame["volume_baseline"] = np.nan
        frame["rvol"] = np.nan

    frame["regime"] = classify_regime(frame, settings.regime)
    return frame


def build_example_machine(settings: Settings) -> SignalStateMachine:
    """State machine wired with the UNVALIDATED example rules."""
    p = settings.rules
    rules = example_ruleset(
        breakout_buffer_pct=p.breakout_buffer_pct,
        retest_tolerance_pct=p.retest_tolerance_pct,
        invalidation_buffer_pct=p.invalidation_buffer_pct,
        confirmation_min_rvol=p.confirmation_min_rvol,
    )
    return SignalStateMachine(rules, settings.state_machine)
