"""Central Pivot Range (CPR) from the previous completed trading session.

    pivot = (H + L + C) / 3
    BC    = (H + L) / 2
    TC    = 2 * pivot - BC
    cpr_top = max(TC, BC); cpr_bottom = min(TC, BC)

H, L, C are the previous session's high, low and close. By default they are
derived from the 5-minute candles themselves; note that NSE's official index
closing value is computed differently (from constituent closing prices) and can
differ slightly from the last candle's close — pass ``daily`` to use official
values instead.

"Previous session" means the previous session *present in the data*. The engine
has no holiday calendar, so a missing day in the data cannot be detected as
missing; a warning is logged when consecutive sessions are > 4 calendar days apart.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..schema import (
    CLOSE_COL,
    EXPECTED_CANDLES_PER_SESSION,
    HIGH_COL,
    LOW_COL,
    TIMESTAMP_COL,
    session_dates,
)

logger = logging.getLogger(__name__)

CPR_COLUMNS = ("cpr_pivot", "cpr_bc", "cpr_tc", "cpr_top", "cpr_bottom", "cpr_width_pct")


def daily_hlc(candles: pd.DataFrame) -> pd.DataFrame:
    """Per-session high, low, close and candle count, indexed by IST session date."""
    sessions = session_dates(candles[TIMESTAMP_COL]).rename("session_date")
    grouped = candles.groupby(sessions, sort=True)
    return pd.DataFrame(
        {
            "high": grouped[HIGH_COL].max(),
            "low": grouped[LOW_COL].min(),
            "close": grouped[CLOSE_COL].last(),
            "n_candles": grouped[CLOSE_COL].size(),
        }
    )


def compute_cpr(
    candles: pd.DataFrame,
    *,
    min_session_candles: int = EXPECTED_CANDLES_PER_SESSION,
    daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """CPR levels for every candle, using only the previous completed session.

    A previous session with fewer than ``min_session_candles`` candles is treated
    as incomplete and yields NaN CPR (no partial-day levels). The current session
    never contributes to its own CPR, so the result is free of look-ahead.
    ``daily`` (optional) overrides derived values; it must be indexed by tz-aware
    IST session date (midnight) with ``high``, ``low``, ``close`` columns and is
    assumed complete. Each session uses the latest ``daily`` row strictly before it.
    """
    if daily is None:
        daily = daily_hlc(candles)
        complete = daily["n_candles"] >= min_session_candles
    else:
        daily = daily.sort_index()
        complete = pd.Series(True, index=daily.index)

    gaps = daily.index.to_series().diff().dt.days
    if (gaps > 4).any():
        logger.warning("session gaps > 4 days before %s; CPR uses the previous session present in data",
                       [str(d.date()) for d in gaps[gaps > 4].index[:5]])

    h = daily["high"].where(complete)
    l = daily["low"].where(complete)
    c = daily["close"].where(complete)
    pivot = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2.0 * pivot - bc
    levels = pd.DataFrame(
        {
            "cpr_pivot": pivot,
            "cpr_bc": bc,
            "cpr_tc": tc,
            "cpr_top": np.maximum(tc, bc),
            "cpr_bottom": np.minimum(tc, bc),
        }
    )
    levels["cpr_width_pct"] = (levels["cpr_top"] - levels["cpr_bottom"]) / levels["cpr_pivot"] * 100.0
    # Each session takes the levels of the latest session strictly before it.
    sessions = session_dates(candles[TIMESTAMP_COL]).rename("session_date")
    levels.index = levels.index.rename("level_session")
    lookup = pd.merge_asof(
        sessions.drop_duplicates().to_frame(),
        levels.reset_index(),
        left_on="session_date",
        right_on="level_session",
        allow_exact_matches=False,
    ).set_index("session_date")

    out = pd.DataFrame(index=candles.index)
    for col in CPR_COLUMNS:
        out[col] = sessions.map(lookup[col]).astype("float64")
    return out
