"""Deterministic SYNTHETIC 5-minute candles for tests and demos.

This is randomly generated data, not market data. Its "volume" imitates a traded
volume series (intraday U-shape) and should be labelled with a traded-volume
:class:`~nifty_signal_engine.schema.VolumeSource` only in tests.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

from .schema import EXPECTED_CANDLES_PER_SESSION, IST, SESSION_OPEN


def generate_synthetic_candles(
    n_sessions: int = 5,
    *,
    start: date = date(2024, 1, 1),
    seed: int = 7,
    base_price: float = 22000.0,
    include_volume: bool = True,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n_sessions)  # weekdays only; no holiday calendar
    rows = []
    price = base_price
    slot = np.arange(EXPECTED_CANDLES_PER_SESSION)
    u_shape = 1.0 + 1.5 * ((slot - slot.mean()) / slot.mean()) ** 2
    for day in days:
        drift = rng.normal(0, 0.0004)  # per-session drift so some days trend
        price *= 1 + rng.normal(0, 0.003)  # overnight gap
        start_ts = pd.Timestamp(datetime.combine(day.date(), SESSION_OPEN), tz=IST)
        for i in range(EXPECTED_CANDLES_PER_SESSION):
            o = round(price, 2)
            c = round(o * (1 + drift + rng.normal(0, 0.0012)), 2)
            h = round(max(o, c) * (1 + abs(rng.normal(0, 0.0006))), 2)
            l = round(min(o, c) * (1 - abs(rng.normal(0, 0.0006))), 2)
            row = {"timestamp": start_ts + pd.Timedelta(minutes=5 * i), "open": o, "high": h, "low": l, "close": c}
            if include_volume:
                row["volume"] = float(round(u_shape[i] * rng.lognormal(11, 0.3)))
            rows.append(row)
            price = c
    return pd.DataFrame(rows)
