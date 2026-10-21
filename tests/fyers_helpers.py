"""Fakes for the FYERS downloader tests. No network, no real credentials."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import pandas as pd

from nifty_signal_engine.fyers import Credentials, FyersHistoryClient, HttpResponse
from nifty_signal_engine.schema import IST

# Obvious fakes. Tests assert these strings never reach logs, errors or files.
FAKE_APP_ID = "FAKEAPPID1-100"
FAKE_TOKEN = "fake-access-token-SECRET-0123456789abcdef"
NOW = datetime(2024, 12, 31, 18, 0, tzinfo=IST)  # after every synthetic candle


def fake_credentials() -> Credentials:
    return Credentials(FAKE_APP_ID, FAKE_TOKEN)


def json_response(payload, status: int = 200, headers=None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(payload).encode())


def ok_payload(rows) -> dict:
    return {"s": "ok", "code": 200, "message": "", "candles": [list(r) for r in rows]}


def frame_to_rows(frame: pd.DataFrame) -> list[list[float]]:
    return [
        [int(t.timestamp()), o, h, l, c, v]
        for t, o, h, l, c, v in zip(frame["timestamp"], frame["open"], frame["high"],
                                    frame["low"], frame["close"], frame["volume"], strict=True)
    ]


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


class FakeFyersApi:
    """Transport serving candles from a DataFrame, honouring range_from/range_to."""

    def __init__(self, frame: pd.DataFrame, *, ignore_range: bool = False,
                 mutate: Callable[[list, int], list] | None = None) -> None:
        self.rows = frame_to_rows(frame)
        self.ignore_range = ignore_range
        self.mutate = mutate
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        q = query(url)
        lo, hi = int(q["range_from"]), int(q["range_to"])
        rows = self.rows if self.ignore_range else [r for r in self.rows if lo <= r[0] <= hi]
        if self.mutate:
            rows = self.mutate(rows, len(self.calls))
        return json_response(ok_payload(rows) if rows else {"s": "no_data", "code": 200, "message": "", "candles": []})


class ScriptedTransport:
    """Returns/raises the scripted items in order, then repeats the last one."""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_client(transport, **kwargs) -> tuple[FyersHistoryClient, list[float]]:
    sleeps: list[float] = []
    kwargs.setdefault("min_interval_seconds", 0.0)
    client = FyersHistoryClient(fake_credentials(), transport=transport, sleep=sleeps.append, **kwargs)
    return client, sleeps
