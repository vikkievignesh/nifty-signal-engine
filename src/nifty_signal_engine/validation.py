"""Validation of candle frames against :mod:`nifty_signal_engine.schema`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .schema import (
    CLOSE_COL,
    EXPECTED_CANDLES_PER_SESSION,
    HIGH_COL,
    LAST_CANDLE_LABEL,
    LOW_COL,
    OHLC_COLS,
    OPEN_COL,
    REQUIRED_COLUMNS,
    SESSION_OPEN,
    TIMESTAMP_COL,
    TIMEZONE_NAME,
    VOLUME_COL,
    session_dates,
)

logger = logging.getLogger(__name__)


class CandleValidationError(ValueError):
    """Raised when a candle frame violates the schema."""


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_errors(self) -> None:
        if self.errors:
            raise CandleValidationError("; ".join(self.errors))


def _first_bad(df: pd.DataFrame, mask: pd.Series) -> str:
    return str(df.loc[mask, TIMESTAMP_COL].iloc[0])


def validate_candles(
    df: pd.DataFrame,
    *,
    require_volume: bool = False,
    check_session_hours: bool = True,
) -> ValidationReport:
    """Check a candle frame and return all problems found (does not raise).

    ``check_session_hours=False`` allows special sessions (e.g. Muhurat trading)
    outside the regular 09:15–15:30 window.
    """
    report = ValidationReport()

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if require_volume and VOLUME_COL not in df.columns:
        missing.append(VOLUME_COL)
    if missing:
        report.errors.append(f"missing columns: {missing}")
        return report
    if df.empty:
        report.errors.append("no candles")
        return report

    ts = df[TIMESTAMP_COL]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        report.errors.append("timestamp must be timezone-aware (Asia/Kolkata)")
        return report
    if str(ts.dtype.tz) != TIMEZONE_NAME:
        report.errors.append(f"timestamp timezone is {ts.dtype.tz}, expected {TIMEZONE_NAME}")
        return report
    if ts.isna().any():
        report.errors.append(f"{int(ts.isna().sum())} missing timestamps")
        return report

    if ts.duplicated().any():
        report.errors.append(f"duplicate timestamps, first at {ts[ts.duplicated()].iloc[0]}")
    if not ts.is_monotonic_increasing:
        report.errors.append("timestamps are not in ascending order")

    off_grid = (ts.dt.minute % 5 != 0) | (ts.dt.second != 0) | (ts.dt.microsecond != 0)
    if off_grid.any():
        report.errors.append(
            f"{int(off_grid.sum())} timestamps not on the 5-minute grid, first at {_first_bad(df, off_grid)}"
        )

    tod = ts.dt.time
    outside = (tod < SESSION_OPEN) | (tod > LAST_CANDLE_LABEL)
    if outside.any():
        msg = f"{int(outside.sum())} candles outside 09:15–15:25 labels, first at {_first_bad(df, outside)}"
        (report.errors if check_session_hours else report.warnings).append(msg)

    weekend = ts.dt.dayofweek >= 5
    if weekend.any():
        report.warnings.append(f"{int(weekend.sum())} candles fall on a weekend")

    for col in OHLC_COLS:
        if not pd.api.types.is_numeric_dtype(df[col]):
            report.errors.append(f"column {col!r} is not numeric")
    if not report.ok:
        return report

    prices = df[list(OHLC_COLS)]
    if prices.isna().any().any():
        report.errors.append(f"{int(prices.isna().any(axis=1).sum())} candles with missing OHLC values")
    if not np.isfinite(prices.fillna(1.0).to_numpy()).all() or (prices <= 0).any().any():
        report.errors.append("OHLC values must be finite and positive")

    o, h, l, c = (df[k] for k in (OPEN_COL, HIGH_COL, LOW_COL, CLOSE_COL))
    checks = {
        "high < low": h < l,
        "high below open/close": h < np.maximum(o, c),
        "low above open/close": l > np.minimum(o, c),
    }
    for name, mask in checks.items():
        if mask.any():
            report.errors.append(f"{int(mask.sum())} candles with {name}, first at {_first_bad(df, mask)}")

    if VOLUME_COL in df.columns:
        vol = df[VOLUME_COL]
        if not pd.api.types.is_numeric_dtype(vol):
            report.errors.append("column 'volume' is not numeric")
        else:
            if (vol < 0).any():
                report.errors.append(f"{int((vol < 0).sum())} candles with negative volume")
            if vol.isna().any():
                report.warnings.append(f"{int(vol.isna().sum())} candles with missing volume")

    counts = ts.groupby(session_dates(ts)).size()
    short = counts[counts != EXPECTED_CANDLES_PER_SESSION]
    if not short.empty:
        report.warnings.append(
            f"{len(short)} sessions without {EXPECTED_CANDLES_PER_SESSION} candles: "
            + ", ".join(f"{d.date()}={n}" for d, n in short.head(5).items())
        )

    for w in report.warnings:
        logger.warning("candle validation: %s", w)
    return report
