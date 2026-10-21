"""OHLCV candle schema for NIFTY 50 five-minute candles.

Canonical in-memory candle frame (one row per candle, ascending time):

==========  ======================================  ===========================
column      dtype                                   meaning
==========  ======================================  ===========================
timestamp   datetime64[*, Asia/Kolkata] (tz-aware)  candle OPEN time (left label)
open        float64                                 first price in the interval
high        float64                                 highest price in the interval
low         float64                                 lowest price in the interval
close       float64                                 last price in the interval
volume      float64 (optional, may be NaN)          volume from ``VolumeSource``
==========  ======================================  ===========================

Timing convention: a candle labelled 09:15 covers [09:15, 09:20) and is only
*complete* — and therefore usable by any signal — at 09:20. Use
:func:`candle_close_time` for the moment a candle's information becomes
available. The regular NSE cash session is 09:15–15:30 IST, giving 75 candles
labelled 09:15 … 15:25.
"""

from __future__ import annotations

from datetime import time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
TIMEZONE_NAME = "Asia/Kolkata"

TIMESTAMP_COL = "timestamp"
OPEN_COL, HIGH_COL, LOW_COL, CLOSE_COL = "open", "high", "low", "close"
OHLC_COLS = (OPEN_COL, HIGH_COL, LOW_COL, CLOSE_COL)
VOLUME_COL = "volume"
REQUIRED_COLUMNS = (TIMESTAMP_COL, *OHLC_COLS)
SCHEMA_COLUMNS = (*REQUIRED_COLUMNS, VOLUME_COL)

CANDLE_INTERVAL = timedelta(minutes=5)
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
LAST_CANDLE_LABEL = time(15, 25)
EXPECTED_CANDLES_PER_SESSION = 75


class VolumeSource(StrEnum):
    """Where the ``volume`` column came from.

    NIFTY 50 is an index, not a traded instrument: "volume" reported on the spot
    index by data vendors is not traded volume of the index and its meaning varies
    by vendor. It is therefore *not* accepted for relative-volume features unless
    explicitly overridden.
    """

    NONE = "none"  # no volume available
    INDEX_REPORTED = "index_reported"  # vendor-reported spot-index volume (unsupported)
    INDEX_FUTURES = "index_futures"  # NIFTY futures contract volume (watch rollovers)
    CONSTITUENT_AGGREGATE = "constituent_aggregate"  # summed constituent stock volume
    ETF_PROXY = "etf_proxy"  # volume of a NIFTY 50 ETF (thin liquidity caveat)


TRADED_VOLUME_SOURCES = frozenset(
    {VolumeSource.INDEX_FUTURES, VolumeSource.CONSTITUENT_AGGREGATE, VolumeSource.ETF_PROXY}
)


def candle_close_time(timestamp: pd.Timestamp) -> pd.Timestamp:
    """Time at which a candle labelled ``timestamp`` is complete."""
    return timestamp + CANDLE_INTERVAL


def session_dates(timestamps: pd.Series) -> pd.Series:
    """IST trading-session date (midnight, tz-aware) for each candle timestamp."""
    return timestamps.dt.normalize()
