from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from nifty_signal_engine.schema import IST
from nifty_signal_engine.synthetic import generate_synthetic_candles

SAMPLE_CSV = Path(__file__).resolve().parents[1] / "data" / "sample" / "synthetic_nifty_5m.csv"


def make_candles(day: str, closes, *, start: str = "09:15", volume=None, spread: float = 1.0) -> pd.DataFrame:
    """Simple candles: open = previous close, high/low = max/min(open, close) +/- spread."""
    ts = pd.date_range(f"{day} {start}", periods=len(closes), freq="5min", tz=IST)
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    df = pd.DataFrame({
        "timestamp": ts,
        "open": opens,
        "high": [max(o, c) + spread for o, c in zip(opens, closes)],
        "low": [min(o, c) - spread for o, c in zip(opens, closes)],
        "close": closes,
    })
    if volume is not None:
        df["volume"] = [float(v) for v in volume]
    return df


@pytest.fixture
def synthetic() -> pd.DataFrame:
    return generate_synthetic_candles(n_sessions=6, seed=11)
