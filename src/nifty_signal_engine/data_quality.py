"""Read-only dataset-level quality audits for historical candles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from .schema import (
    CANDLE_INTERVAL,
    EXPECTED_CANDLES_PER_SESSION,
    OHLC_COLS,
    SESSION_OPEN,
    LAST_CANDLE_LABEL,
    TIMESTAMP_COL,
    VOLUME_COL,
)


def audit_dataset(
    df: pd.DataFrame,
    *,
    volume_source: str = "unknown",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize candle quality without changing the input DataFrame.

    Missing calendar dates are not classified as missing sessions because
    this audit does not apply an exchange holiday calendar.
    """
    report: dict[str, Any] = {
        "rows": len(df),
        "errors": [],
        "warnings": [],
        "info": [],
        "sessions": [],
        "duplicate_timestamps": 0,
        "missing_values": {},
        "repeated_ohlcv_transitions": 0,
        "metadata_checks": {},
        "volume_source": volume_source,
    }

    required = [TIMESTAMP_COL, *OHLC_COLS]
    missing_columns = [c for c in required if c not in df.columns]
    if missing_columns:
        report["errors"].append(f"Missing required columns: {missing_columns}")
        return report

    if df.empty:
        report["errors"].append("Dataset contains no candles")
        return report

    # Work on a copy; never mutate the caller's DataFrame.
    work = df.copy()
    ts = work[TIMESTAMP_COL]

    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        report["errors"].append("Timestamps must be timezone-aware")
        return report

    if ts.isna().any():
        report["errors"].append(f"{int(ts.isna().sum())} missing timestamps")
        return report

    report["first_timestamp"] = str(ts.min())
    report["last_timestamp"] = str(ts.max())
    report["duplicate_timestamps"] = int(ts.duplicated(keep=False).sum())

    if report["duplicate_timestamps"]:
        report["warnings"].append(
            f"{report['duplicate_timestamps']} rows belong to duplicate timestamp groups"
        )

    if not ts.is_monotonic_increasing:
        report["warnings"].append("Timestamps are not in ascending order")

    for col in [*OHLC_COLS, *([VOLUME_COL] if VOLUME_COL in work else [])]:
        report["missing_values"][col] = int(work[col].isna().sum())

    # Basic OHLC checks.
    for col in OHLC_COLS:
        numeric = pd.to_numeric(work[col], errors="coerce")
        if numeric.isna().any():
            report["errors"].append(f"{col} contains missing or non-numeric values")
        elif not numeric.map(lambda value: pd.notna(value) and abs(value) != float("inf")).all():
            report["errors"].append(f"{col} contains non-finite values")

    if not report["errors"]:
        o, h, l, c = (work[col] for col in OHLC_COLS)
        bad = (h < l) | (h < o) | (h < c) | (l > o) | (l > c)
        if bad.any():
            report["errors"].append(f"{int(bad.sum())} candles violate OHLC relationships")

    if VOLUME_COL in work:
        volume = pd.to_numeric(work[VOLUME_COL], errors="coerce")
        report["zero_volume_candles"] = int((volume == 0).sum())
        report["negative_volume_candles"] = int((volume < 0).sum())
        if report["zero_volume_candles"]:
            report["warnings"].append(
                f"{report['zero_volume_candles']} candles have zero volume"
            )
        if report["negative_volume_candles"]:
            report["errors"].append(
                f"{report['negative_volume_candles']} candles have negative volume"
            )
    else:
        report["zero_volume_candles"] = None
        report["info"].append("No volume column is available")

    if volume_source == "index_reported":
        report["warnings"].append(
            "Spot-index reported volume is not validated traded volume; "
            "do not use it for relative-volume research"
        )

    # Candle-pattern diagnostics. These are warnings, not automatic deletions.
    prices = work[list(OHLC_COLS)]
    flat = prices.nunique(axis=1, dropna=False) == 1
    report["flat_candles"] = int(flat.sum())
    if flat.any():
        report["warnings"].append(
            f"{report['flat_candles']} flat candles (OHLC values are identical)"
        )

    adjacent = ts.diff().eq(pd.Timedelta(CANDLE_INTERVAL))

    # Repeated prices can matter even when vendor-reported volume changes.
    same_ohlc = prices.eq(prices.shift()).all(axis=1)
    continues_ohlc = same_ohlc & adjacent
    repeated_ohlc = continues_ohlc
    report["repeated_ohlc_transitions"] = int(repeated_ohlc.sum())
    if repeated_ohlc.any():
        report["warnings"].append(
            f"{int(repeated_ohlc.sum())} repeated OHLC transitions "
            "(adjacent candles have identical OHLC values)"
        )

    # Exact OHLCV repeats are a separate, stricter diagnostic.
    report["repeated_ohlcv_transitions"] = 0
    if VOLUME_COL in work:
        repeated_cols = [*OHLC_COLS, VOLUME_COL]
        same_ohlcv = work[repeated_cols].eq(work[repeated_cols].shift()).all(axis=1)
        continues_ohlcv = same_ohlcv & adjacent
        ohlcv_run_id = (~continues_ohlcv).cumsum()
        ohlcv_run_size = continues_ohlcv.groupby(ohlcv_run_id).transform("sum")
        repeated_ohlcv = continues_ohlcv & (ohlcv_run_size >= 4)
        report["repeated_ohlcv_transitions"] = int(repeated_ohlcv.sum())
        if repeated_ohlcv.any():
            report["warnings"].append(
                f"{int(repeated_ohlcv.sum())} repeated OHLCV transitions within "
                "runs of at least 5 identical consecutive candles"
            )

    # Session completeness: only dates present in the file are assessed.
    work["_audit_session"] = ts.dt.normalize()
    work["_audit_time"] = ts.dt.time
    session_counts = work.groupby("_audit_session", sort=True).size()

    for session, count in session_counts.items():
        session_rows = work.loc[work["_audit_session"] == session]
        labels = session_rows[TIMESTAMP_COL].dt.time
        outside = (labels < SESSION_OPEN) | (labels > LAST_CANDLE_LABEL)
        unique_times = session_rows[TIMESTAMP_COL].drop_duplicates()
        expected = pd.date_range(
            start=session + pd.Timedelta(
                hours=SESSION_OPEN.hour, minutes=SESSION_OPEN.minute
            ),
            periods=EXPECTED_CANDLES_PER_SESSION,
            freq=CANDLE_INTERVAL,
        )
        actual_set = set(unique_times)
        missing = [str(t) for t in expected if t not in actual_set]
        report["sessions"].append(
            {
                "date": str(session.date()),
                "rows": int(count),
                "expected_rows": EXPECTED_CANDLES_PER_SESSION,
                "missing_expected_candles": len(missing),
                "outside_regular_session": int(outside.sum()),
                "missing_timestamps": missing,
            }
        )

    incomplete = [s for s in report["sessions"] if s["rows"] != s["expected_rows"]]
    if incomplete:
        report["warnings"].append(
            f"{len(incomplete)} observed sessions do not contain "
            f"{EXPECTED_CANDLES_PER_SESSION} rows"
        )

    # Metadata checks are informational comparisons, not data repairs.
    if metadata is not None:
        checks = report["metadata_checks"]
        if "rows" in metadata:
            checks["metadata_rows_match"] = metadata["rows"] == len(df)
        if "sessions" in metadata:
            checks["metadata_sessions_match"] = metadata["sessions"] == len(session_counts)
        if "volume_source" in metadata:
            checks["metadata_volume_source"] = metadata["volume_source"]
            if metadata["volume_source"] != volume_source:
                report["warnings"].append(
                    "Provided volume_source differs from metadata volume_source"
                )

    report["info"].append(
        "Dates absent from the dataset are not labelled missing sessions; "
        "no exchange holiday calendar is applied."
    )
    return report


def compare_datasets(
    left: pd.DataFrame,
    right: pd.DataFrame,
) -> dict[str, Any]:
    """Compare timestamp overlap and OHLCV values without merging the source files."""
    required = [TIMESTAMP_COL, *OHLC_COLS]
    for name, frame in (("left", left), ("right", right)):
        missing = [c for c in required if c not in frame.columns]
        if missing:
            raise ValueError(f"{name} dataset is missing columns: {missing}")
        if frame[TIMESTAMP_COL].duplicated().any():
            raise ValueError(f"{name} dataset contains duplicate timestamps")

    common = sorted(set(left[TIMESTAMP_COL]) & set(right[TIMESTAMP_COL]))
    result: dict[str, Any] = {
        "left_rows": len(left),
        "right_rows": len(right),
        "overlapping_timestamps": len(common),
        "ohlcv_matches": {},
        "candles_with_any_difference": None,
    }

    if not common:
        result["candles_with_any_difference"] = 0
        return result

    columns = [*OHLC_COLS]
    if VOLUME_COL in left.columns and VOLUME_COL in right.columns:
        columns.append(VOLUME_COL)

    a = left.set_index(TIMESTAMP_COL).loc[common, columns]
    b = right.set_index(TIMESTAMP_COL).loc[common, columns]
    any_difference = pd.Series(False, index=a.index)

    for col in columns:
        equal = a[col].eq(b[col]) | (a[col].isna() & b[col].isna())
        result["ohlcv_matches"][col] = int(equal.sum())
        any_difference |= ~equal

    result["candles_with_any_difference"] = int(any_difference.sum())
    return result
