
"""Central Pivot Range (CPR) calculations.

Daily CPR uses the previous completed observed session.
Weekly and monthly CPR use the previous calendar period.

This module does not validate completeness against an NSE trading calendar.
Missing expected sessions cannot be detected without trusted metadata.
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

CPR_COLUMNS = (
    "cpr_pivot",
    "cpr_bc",
    "cpr_tc",
    "cpr_top",
    "cpr_bottom",
    "cpr_width_pct",
)


def _validate_candles(candles: pd.DataFrame) -> None:
    """Validate timestamp ordering required for CPR calculations."""
    if TIMESTAMP_COL not in candles.columns:
        raise ValueError(f"candles must contain {TIMESTAMP_COL!r}")

    timestamps = candles[TIMESTAMP_COL]
    if not timestamps.is_monotonic_increasing or timestamps.duplicated().any():
        raise ValueError("candles must be in strictly ascending time order")


def _levels_from_hlc(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> pd.DataFrame:
    """Calculate CPR levels from high, low and close series."""
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
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
    levels["cpr_width_pct"] = (
        (levels["cpr_top"] - levels["cpr_bottom"])
        / levels["cpr_pivot"]
        * 100.0
    )
    return levels.loc[:, CPR_COLUMNS]


def daily_hlc(candles: pd.DataFrame) -> pd.DataFrame:
    """Return each observed IST session's high, low, close and candle count."""
    _validate_candles(candles)

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
    """Calculate daily CPR from the previous completed observed session.

    Sessions with fewer than min_session_candles are treated as incomplete.
    A supplied daily DataFrame is assumed complete and must contain high, low
    and close columns indexed by IST session dates.
    """
    _validate_candles(candles)

    if candles.empty:
        return pd.DataFrame(index=candles.index, columns=CPR_COLUMNS, dtype=float)

    if min_session_candles < 1:
        raise ValueError("min_session_candles must be at least 1")

    if daily is None:
        daily = daily_hlc(candles).copy()
        complete = daily["n_candles"] >= min_session_candles
    else:
        daily = daily.sort_index().copy()
        required = {"high", "low", "close"}
        if not required.issubset(daily.columns):
            raise ValueError("daily must contain high, low and close columns")
        complete = pd.Series(True, index=daily.index)

    gaps = daily.index.to_series().diff().dt.days
    if (gaps > 4).any():
        logger.warning(
            "Session gaps > 4 days; CPR uses the previous session present "
            "in the data: %s",
            [str(d.date()) for d in gaps[gaps > 4].index[:5]],
        )

    levels = _levels_from_hlc(
        daily["high"].where(complete),
        daily["low"].where(complete),
        daily["close"].where(complete),
    )
    levels.index = levels.index.rename("level_session")

    sessions = session_dates(candles[TIMESTAMP_COL]).rename("session_date")
    lookup = pd.merge_asof(
        sessions.drop_duplicates().to_frame(),
        levels.reset_index(),
        left_on="session_date",
        right_on="level_session",
        allow_exact_matches=False,
    ).set_index("session_date")

    out = pd.DataFrame(index=candles.index, columns=CPR_COLUMNS, dtype=float)
    for column in CPR_COLUMNS:
        out[column] = sessions.map(lookup[column]).astype(float)

    return out


def _compute_period_cpr(
    candles: pd.DataFrame,
    *,
    period: str,
    min_session_candles: int = EXPECTED_CANDLES_PER_SESSION,
) -> pd.DataFrame:
    """Calculate CPR from the previous calendar week or month.

    Every observed session in a period must meet the candle-count threshold.
    Missing expected trading sessions cannot be identified without an exchange
    calendar or trusted completeness metadata.
    """
    if period not in {"week", "month"}:
        raise ValueError("period must be 'week' or 'month'")
    if min_session_candles < 1:
        raise ValueError("min_session_candles must be at least 1")

    _validate_candles(candles)

    if candles.empty:
        return pd.DataFrame(index=candles.index, columns=CPR_COLUMNS, dtype=float)

    daily = daily_hlc(candles)
    session_index = pd.DatetimeIndex(daily.index).tz_localize(None)
    frequency = "W-SUN" if period == "week" else "M"
    keys = session_index.to_period(frequency)

    period_frame = daily.copy()
    period_frame["_period"] = keys
    period_frame["_complete"] = (
        daily["n_candles"] >= min_session_candles
    ).to_numpy()

    grouped = period_frame.groupby("_period", sort=True).agg(
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        complete=("_complete", "all"),
    )

    grouped.loc[
        ~grouped["complete"], ["high", "low", "close"]
    ] = np.nan
    grouped = grouped.drop(columns="complete")

    levels = _levels_from_hlc(
        grouped["high"],
        grouped["low"],
        grouped["close"],
    )

    # Preserve missing calendar periods. Never reuse older levels across gaps.
    full_period_index = pd.period_range(
        start=keys.min(),
        end=keys.max(),
        freq=frequency,
    )
    levels = levels.reindex(full_period_index).shift(1)

    candle_dates = session_dates(candles[TIMESTAMP_COL])
    candle_periods = (
        pd.DatetimeIndex(candle_dates)
        .tz_localize(None)
        .to_period(frequency)
    )

    mapped = levels.reindex(candle_periods)
    mapped.index = candles.index
    return mapped.loc[:, CPR_COLUMNS]


def compute_weekly_cpr(
    candles: pd.DataFrame,
    *,
    min_session_candles: int = EXPECTED_CANDLES_PER_SESSION,
) -> pd.DataFrame:
    """Weekly CPR from the previous calendar week."""
    return _compute_period_cpr(
        candles,
        period="week",
        min_session_candles=min_session_candles,
    )


def compute_monthly_cpr(
    candles: pd.DataFrame,
    *,
    min_session_candles: int = EXPECTED_CANDLES_PER_SESSION,
) -> pd.DataFrame:
    """Monthly CPR from the previous calendar month."""
    return _compute_period_cpr(
        candles,
        period="month",
        min_session_candles=min_session_candles,
    )
