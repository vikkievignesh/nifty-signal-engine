"""Download NIFTY 50 five-minute candles from FYERS into a local, git-ignored CSV.

Pipeline: split the date range into windows within the FYERS intraday limit ->
fetch each window -> keep the raw response -> convert epoch seconds to Asia/Kolkata
-> drop not-yet-complete candles and out-of-range candles -> de-duplicate -> run
the project's ``validate_candles`` -> write CSV + metadata -> re-load the CSV with
``load_candles_csv`` to prove it round-trips through the existing loader.

VOLUME WARNING: ``NSE:NIFTY50-INDEX`` is a spot index. Any ``volume`` FYERS returns
for it must NOT be assumed to be genuine traded volume (it is often zero or
vendor-defined). The CSV keeps the column as received, the metadata records
``volume_source = "index_reported"``, and the engine's relative-volume feature
refuses that source. Use futures / constituent / ETF volume for volume research.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..data_loader import load_candles_csv
from ..schema import (
    CANDLE_INTERVAL,
    CLOSE_COL,
    EXPECTED_CANDLES_PER_SESSION,
    HIGH_COL,
    IST,
    LOW_COL,
    OPEN_COL,
    SCHEMA_COLUMNS,
    SESSION_OPEN,
    TIMESTAMP_COL,
    VOLUME_COL,
    VolumeSource,
)
from ..validation import validate_candles
from .client import FyersHistoryClient
from .errors import FyersApiError, NoDataError

logger = logging.getLogger(__name__)

NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"
RESOLUTION = "5"
# FYERS documents "up to 100 days of data per request" for intraday resolutions
# (1..240 minutes). Windows default to 90 calendar days to stay clear of an
# inclusive-boundary off-by-one.
MAX_WINDOW_DAYS = 100
DEFAULT_WINDOW_DAYS = 90
VOLUME_WARNING = (
    "volume comes from a spot index (NSE:NIFTY50-INDEX) and must not be assumed to be genuine "
    "traded volume; do not use it for relative-volume research"
)
_MIN_EPOCH, _MAX_EPOCH = 1e8, 4e9  # rejects millisecond epochs and garbage
# The 75 candle labels of a regular session (09:15 .. 15:25), used to recognise a *full* session.
_SESSION_SLOTS = frozenset(
    (datetime.combine(date.min, SESSION_OPEN) + i * CANDLE_INTERVAL).time()
    for i in range(EXPECTED_CANDLES_PER_SESSION)
)
_MAX_DATES_IN_WARNING = 10


@dataclass(frozen=True)
class DownloadResult:
    csv_path: Path
    meta_path: Path
    raw_dir: Path | None
    frame: pd.DataFrame
    metadata: dict[str, Any]


def split_date_range(start: date, end: date, window_days: int = DEFAULT_WINDOW_DAYS) -> list[tuple[date, date]]:
    """Split ``[start, end]`` (inclusive calendar dates) into contiguous windows.

    Each window spans at most ``window_days`` calendar days, never more than
    :data:`MAX_WINDOW_DAYS`; windows neither overlap nor leave gaps.
    """
    if not 1 <= window_days <= MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {MAX_WINDOW_DAYS}, got {window_days}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _ist_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=IST)


def _window_epochs(window_start: date, window_end: date) -> tuple[int, int]:
    """Epoch seconds covering ``window_start`` 00:00 IST .. ``window_end`` 23:59:59 IST."""
    return (
        int(_ist_midnight(window_start).timestamp()),
        int((_ist_midnight(window_end + timedelta(days=1)) - timedelta(seconds=1)).timestamp()),
    )


def _rows_from_payload(payload: dict[str, Any]) -> list[Sequence[float]]:
    """Validate and return ``[epoch, open, high, low, close, volume]`` rows."""
    candles = payload.get("candles")
    if candles is None:
        return []
    if not isinstance(candles, list):
        raise FyersApiError("FYERS 'candles' field is not a list")
    rows = []
    for position, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise FyersApiError(f"malformed candle at position {position}: expected 6 values")
        values = row[:6]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
            raise FyersApiError(f"malformed candle at position {position}: non-numeric or non-finite value")
        if not _MIN_EPOCH <= values[0] <= _MAX_EPOCH:
            raise FyersApiError(f"candle {position} timestamp is not epoch seconds")
        rows.append(values)
    return rows


def _frame_from_rows(rows: Sequence[Sequence[float]]) -> pd.DataFrame:
    raw = pd.DataFrame(list(rows), columns=["epoch", OPEN_COL, HIGH_COL, LOW_COL, CLOSE_COL, VOLUME_COL])
    frame = pd.DataFrame({TIMESTAMP_COL: pd.to_datetime(raw["epoch"], unit="s", utc=True).dt.tz_convert(IST)})
    for column in (OPEN_COL, HIGH_COL, LOW_COL, CLOSE_COL, VOLUME_COL):
        frame[column] = raw[column].astype("float64")
    return frame[list(SCHEMA_COLUMNS)]


def _duplicates_by_date(
    frame: pd.DataFrame, same_timestamp: pd.Series, conflict_mask: pd.Series
) -> dict[str, dict[str, Any]]:
    """Classify repeated timestamps per IST date (call before the duplicates are dropped).

    ``frame`` is sorted by timestamp and the masks are aligned with it. Per date this reports the rows that
    will be dropped, how many of them conflict with the first occurrence, and ``full_session_repeat``: True only
    when the date has no conflicting rows, its timestamps are exactly the 75 regular-session slots, and every
    one of those timestamps appears exactly twice. Based on counts, so it does not depend on row order/layout.
    """
    day = frame[TIMESTAMP_COL].dt.strftime("%Y-%m-%d")
    report: dict[str, dict[str, Any]] = {}
    for date_text in sorted(day[same_timestamp].unique()):
        on_day = day == date_text
        per_timestamp = frame[on_day].groupby(TIMESTAMP_COL).size()
        conflicting = int(conflict_mask[on_day].sum())
        full = (conflicting == 0 and bool((per_timestamp == 2).all())
                and {stamp.time() for stamp in per_timestamp.index} == _SESSION_SLOTS)
        report[date_text] = {"dropped": int(same_timestamp[on_day].sum()), "conflicting": conflicting,
                             "full_session_repeat": full}
    return report


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save_raw(raw_dir: Path, stem: str, window: tuple[date, date], request: dict[str, Any],
              payload: dict[str, Any], client_redact) -> None:
    document = {
        "note": "Raw FYERS history response. No credentials or headers are stored.",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": request,
        "response": payload,
    }
    text = client_redact(json.dumps(document, indent=2, sort_keys=True))
    _write_atomic(raw_dir / f"{stem}_{window[0]}_{window[1]}.json", text)


def download_nifty_5m(
    start: date,
    end: date,
    *,
    client: FyersHistoryClient,
    out_dir: str | Path,
    window_days: int = DEFAULT_WINDOW_DAYS,
    save_raw: bool = True,
    allow_off_hours: bool = False,
    now: datetime | None = None,
    symbol: str = NIFTY_SYMBOL,
) -> DownloadResult:
    """Download, clean, validate and store candles for ``[start, end]`` (IST dates).

    Raises ``NoDataError`` if nothing came back, ``CandleValidationError`` if the
    assembled data violates the schema (nothing is written then; raw responses are
    kept), and the ``Fyers*Error`` types from the client for API failures.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    if start > now.date():
        raise ValueError(f"start {start} is in the future")
    end = min(end, now.date())
    windows = split_date_range(start, end, window_days)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.replace(':', '_')}_{RESOLUTION}m_{start}_{end}"
    raw_dir = out_dir / "raw" if save_raw else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
    redact = client.redact  # raw files are scrubbed of credentials as a safety net

    all_rows: list[Sequence[float]] = []
    empty_windows: list[str] = []
    for index, window in enumerate(windows, start=1):
        range_from, range_to = _window_epochs(*window)
        payload = client.fetch_history(symbol, RESOLUTION, range_from, range_to)
        if raw_dir:
            request = {"symbol": symbol, "resolution": RESOLUTION, "date_format": 0, "cont_flag": 1,
                       "range_from": range_from, "range_to": range_to,
                       "window_start": str(window[0]), "window_end": str(window[1])}
            _save_raw(raw_dir, stem, window, request, payload, redact)
        rows = _rows_from_payload(payload)
        logger.info("window %d/%d %s..%s: %d candles", index, len(windows), window[0], window[1], len(rows))
        if not rows:
            empty_windows.append(f"{window[0]}..{window[1]}")
            logger.warning("window %s..%s returned no candles", window[0], window[1])
        all_rows.extend(rows)

    if not all_rows:
        raise NoDataError(f"FYERS returned no candles for {start}..{end}"
                          + (f"; raw responses kept in {raw_dir}" if raw_dir else ""))

    frame = _frame_from_rows(all_rows)
    received = len(frame)

    in_range = (frame[TIMESTAMP_COL] >= pd.Timestamp(_ist_midnight(start))) & (
        frame[TIMESTAMP_COL] < pd.Timestamp(_ist_midnight(end + timedelta(days=1))))
    out_of_range = int((~in_range).sum())
    frame = frame[in_range]

    complete = frame[TIMESTAMP_COL] + CANDLE_INTERVAL <= pd.Timestamp(now)
    incomplete_dropped = int((~complete).sum())
    frame = frame[complete]

    frame = frame.sort_values(TIMESTAMP_COL, kind="stable").reset_index(drop=True)
    same_timestamp = frame.duplicated(TIMESTAMP_COL, keep="first")
    identical = frame.duplicated(keep="first")
    conflict_mask = same_timestamp & ~identical
    conflicting = int(conflict_mask.sum())
    duplicates_dropped = int(same_timestamp.sum())
    duplicates = _duplicates_by_date(frame, same_timestamp, conflict_mask)
    frame = frame[~same_timestamp].reset_index(drop=True)
    if out_of_range:
        logger.warning("dropped %d candles outside the requested date range", out_of_range)
    if incomplete_dropped:
        logger.info("dropped %d not-yet-complete candle(s)", incomplete_dropped)
    if duplicates_dropped:
        logger.warning("dropped %d duplicate candle(s), %d with conflicting values (first kept)",
                       duplicates_dropped, conflicting)
    repeated = [date_text for date_text, entry in duplicates.items() if entry["full_session_repeat"]]
    if repeated:
        shown = ", ".join(repeated[:_MAX_DATES_IN_WARNING])
        if len(repeated) > _MAX_DATES_IN_WARNING:
            shown += f" and {len(repeated) - _MAX_DATES_IN_WARNING} more"
        logger.warning("FYERS returned %d full trading session(s) twice (%s) with identical values; dropped %d "
                       "duplicate candle(s) and kept one copy of each. The raw response is preserved.",
                       len(repeated), shown, sum(duplicates[d]["dropped"] for d in repeated))
    if frame.empty:
        raise NoDataError(f"no complete candles remain for {start}..{end} after filtering")
    pre_session_candles_dropped = 0

    if not allow_off_hours:
       pre_session = frame[TIMESTAMP_COL].dt.time < SESSION_OPEN
       pre_session_candles_dropped = int(pre_session.sum())
       frame = frame.loc[~pre_session].reset_index(drop=True)

    report = validate_candles(frame, check_session_hours=not allow_off_hours)
    report.raise_if_errors()

    counts = frame.groupby(frame[TIMESTAMP_COL].dt.normalize()).size()
    sessions_present = {ts.date() for ts in counts.index}
    weekdays = {ts.date() for ts in pd.bdate_range(start, end)}
    metadata: dict[str, Any] = {
        "source": "FYERS API v3 /data/history",
        "symbol": symbol,
        "resolution_minutes": int(RESOLUTION),
        "requested_start": str(start),
        "requested_end": str(end),
        "windows": len(windows),
        "empty_windows": empty_windows,
        "rows": int(len(frame)),
        "rows_received": int(received),
        "first_candle": frame[TIMESTAMP_COL].iloc[0].isoformat(),
        "last_candle": frame[TIMESTAMP_COL].iloc[-1].isoformat(),
        "sessions": int(len(counts)),
        "incomplete_sessions": {ts.date().isoformat(): int(n) for ts, n in counts.items()
                                if n != EXPECTED_CANDLES_PER_SESSION},
        "weekdays_without_data": sorted(d.isoformat() for d in weekdays - sessions_present),
        "weekdays_without_data_note": "includes exchange holidays; no holiday calendar is applied",
        "duplicates_dropped": duplicates_dropped,
        "conflicting_duplicates": conflicting,
        "duplicates_by_date": duplicates,
        "out_of_range_dropped": out_of_range,
        "incomplete_candles_dropped": incomplete_dropped,
        "pre_session_candles_dropped": pre_session_candles_dropped,
        "validation_warnings": report.warnings,
        "volume_source": VolumeSource.INDEX_REPORTED.value,
        "volume_warning": VOLUME_WARNING,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    csv_path = out_dir / f"{stem}.csv"
    meta_path = out_dir / f"{stem}.meta.json"
    out = frame.copy()
    out[TIMESTAMP_COL] = [t.isoformat() for t in out[TIMESTAMP_COL]]
    _write_atomic(csv_path, out.to_csv(index=False))
    _write_atomic(meta_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    loaded = load_candles_csv(csv_path, require_volume=True, check_session_hours=not allow_off_hours)
    logger.info("wrote %d candles to %s", len(loaded), csv_path)
    return DownloadResult(csv_path, meta_path, raw_dir, loaded, metadata)
