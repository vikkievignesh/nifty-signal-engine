"""Historical candle CSV loading."""

from __future__ import annotations

import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .schema import IST, OHLC_COLS, SCHEMA_COLUMNS, TIMESTAMP_COL, VOLUME_COL
from .validation import CandleValidationError, validate_candles

logger = logging.getLogger(__name__)

_OFFSET_PATTERN = r"(?:Z|[+-]\d{2}:?\d{2})$"


def parse_timestamps(raw: pd.Series, naive_timezone: ZoneInfo = IST) -> pd.Series:
    """Parse ISO-8601 timestamp strings into tz-aware Asia/Kolkata timestamps.

    Strings with a UTC offset are converted to IST. Strings without an offset are
    interpreted in ``naive_timezone`` (IST by default). A mix of both is rejected
    because it usually signals a data-assembly mistake.
    """
    text = raw.astype(str).str.strip()
    has_offset = text.str.contains(_OFFSET_PATTERN, regex=True)
    try:
        if has_offset.all():
            parsed = pd.to_datetime(text, format="ISO8601", utc=True)
        elif not has_offset.any():
            parsed = pd.to_datetime(text, format="ISO8601").dt.tz_localize(naive_timezone)
        else:
            raise CandleValidationError("timestamp column mixes values with and without UTC offsets")
    except CandleValidationError:
        raise
    except ValueError as exc:
        raise CandleValidationError(f"unparseable timestamps (ISO-8601 required): {str(exc).splitlines()[0]}") from exc
    return parsed.dt.tz_convert(IST)


def load_candles_csv(
    path: str | Path,
    *,
    naive_timezone: ZoneInfo = IST,
    require_volume: bool = False,
    check_session_hours: bool = True,
) -> pd.DataFrame:
    """Load a 5-minute OHLCV CSV into the canonical schema and validate it.

    Column names are matched case-insensitively; extra columns are dropped. Rows
    are *not* re-sorted or de-duplicated — such problems are reported as errors so
    they are fixed at the source rather than hidden.
    """
    path = Path(path)
    try:
        raw = pd.read_csv(path)
    except pd.errors.EmptyDataError as exc:
        raise CandleValidationError(f"{path}: file is empty") from exc
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    duplicated = sorted(set(raw.columns[raw.columns.duplicated()]))
    if duplicated:
        raise CandleValidationError(f"{path}: duplicate column names after lower-casing: {duplicated}")

    missing = [c for c in (TIMESTAMP_COL, *OHLC_COLS) if c not in raw.columns]
    if missing:
        raise CandleValidationError(f"{path}: missing columns {missing}")

    try:
        df = pd.DataFrame({TIMESTAMP_COL: parse_timestamps(raw[TIMESTAMP_COL], naive_timezone)})
    except CandleValidationError as exc:
        raise CandleValidationError(f"{path}: {exc}") from exc
    for col in OHLC_COLS:
        df[col] = pd.to_numeric(raw[col], errors="coerce").astype("float64")
    if VOLUME_COL in raw.columns:
        df[VOLUME_COL] = pd.to_numeric(raw[VOLUME_COL], errors="coerce").astype("float64")
    df = df[[c for c in SCHEMA_COLUMNS if c in df.columns]]

    report = validate_candles(df, require_volume=require_volume, check_session_hours=check_session_hours)
    if not report.ok:
        raise CandleValidationError(f"{path}: " + "; ".join(report.errors))
    logger.info("loaded %d candles from %s (%s .. %s)", len(df), path,
                df[TIMESTAMP_COL].iloc[0], df[TIMESTAMP_COL].iloc[-1])
    return df
