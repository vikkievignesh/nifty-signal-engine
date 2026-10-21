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


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """No test may open a non-loopback connection (e.g. to FYERS), even by accident."""
    import socket

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    loopback = {"127.0.0.1", "::1", "localhost"}

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in loopback:
            raise RuntimeError(f"test attempted a non-loopback connection to {host!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in loopback:
            raise RuntimeError(f"test attempted DNS resolution of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
