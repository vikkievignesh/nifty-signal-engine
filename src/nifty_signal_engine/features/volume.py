"""Relative volume (RVOL) with baselines that exclude the current candle.

Supported volume sources are listed in
:data:`nifty_signal_engine.schema.TRADED_VOLUME_SOURCES`. Vendor-reported NIFTY
spot-index volume is *not* a traded-volume measure and is rejected unless the
caller explicitly opts in.

Modes
-----
``rolling``      baseline = mean volume of the previous ``lookback`` candles
                 (continuous across sessions — the opening candles are compared
                 with the previous afternoon, which biases RVOL upward at the open).
``time_of_day``  baseline = mean volume of the same intraday slot over the
                 previous ``lookback`` sessions (accounts for the intraday U-shape).
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import pandas as pd

from ..schema import TIMESTAMP_COL, TRADED_VOLUME_SOURCES, VOLUME_COL, VolumeSource


class RelativeVolumeMode(StrEnum):
    ROLLING = "rolling"
    TIME_OF_DAY = "time_of_day"


class UnsupportedVolumeSourceError(ValueError):
    """Raised when RVOL is requested on a volume source that is not traded volume."""


def relative_volume(
    candles: pd.DataFrame,
    *,
    source: VolumeSource,
    lookback: int = 20,
    mode: RelativeVolumeMode = RelativeVolumeMode.ROLLING,
    allow_unsupported_source: bool = False,
) -> pd.DataFrame:
    """Return ``volume_baseline`` and ``rvol`` columns aligned with ``candles``.

    ``rvol = volume / baseline``; NaN during warm-up or when the baseline is <= 0.
    """
    source = VolumeSource(source)
    mode = RelativeVolumeMode(mode)
    if source not in TRADED_VOLUME_SOURCES and not allow_unsupported_source:
        raise UnsupportedVolumeSourceError(
            f"volume source {source.value!r} is not a traded-volume measure; "
            f"supported: {sorted(s.value for s in TRADED_VOLUME_SOURCES)}"
        )
    if VOLUME_COL not in candles.columns:
        raise ValueError("candles have no 'volume' column")
    if not isinstance(lookback, int) or lookback < 1:
        raise ValueError(f"lookback must be a positive integer, got {lookback!r}")

    vol = candles[VOLUME_COL].astype("float64")
    if mode is RelativeVolumeMode.ROLLING:
        baseline = vol.shift(1).rolling(lookback, min_periods=lookback).mean()
    else:
        ts = candles[TIMESTAMP_COL]
        slot = ts.dt.hour * 60 + ts.dt.minute
        baseline = vol.groupby(slot).transform(
            lambda s: s.shift(1).rolling(lookback, min_periods=lookback).mean()
        )

    rvol = vol / baseline.where(baseline > 0)
    return pd.DataFrame({"volume_baseline": baseline, "rvol": rvol.replace([np.inf, -np.inf], np.nan)},
                        index=candles.index)
