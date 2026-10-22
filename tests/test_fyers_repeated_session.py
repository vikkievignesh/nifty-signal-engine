"""Regression: one FYERS history response contained a whole session twice.

Confirmed pattern from a real run (a single request, one response of 375 rows): 15 Oct (75 rows), 16 Oct (75),
19 Oct (75) and 21 Oct (150) -- the last being two contiguous, ascending copies of the same 75-candle session with
identical OHLCV. 20 and 22 Oct have no data. The downloader must keep exactly one copy, report the counts, leave
every retained value untouched, and keep the raw response un-deduplicated for debugging.

All data below is synthetic (seeded, network-free); only the shape of the real response is reproduced.
"""

import json
from datetime import date, datetime
from itertools import groupby

import pandas as pd
import pytest

from nifty_signal_engine.fyers import download_nifty_5m
from nifty_signal_engine.schema import IST
from nifty_signal_engine.synthetic import generate_synthetic_candles

from fyers_helpers import FakeFyersApi, frame_to_rows, make_client

RUN_TIME = datetime(2026, 10, 22, 18, 0, tzinfo=IST)  # after every candle (the shared NOW helper is in 2024)
SESSION_DAYS = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-21"]
FULL_SESSION = [t.strftime("%H:%M") for t in pd.date_range("2026-10-21 09:15", "2026-10-21 15:25", freq="5min")]


@pytest.fixture(scope="module")
def sessions():
    """Four complete sessions (15, 16, 19, 21 Oct 2026) with the flat/zero-volume end-of-session candles seen live."""
    frame = generate_synthetic_candles(6, start=date(2026, 10, 15), seed=3)  # weekdays 15, 16, 19, 20, 21, 22
    frame = frame[~frame["timestamp"].dt.day.isin([20, 22])].reset_index(drop=True)
    label = frame["timestamp"].dt.strftime("%H:%M")
    for hm in ("15:15", "15:20"):
        frame.loc[label == hm, ["open", "high", "low"]] = frame.loc[label == hm, ["close"] * 3].to_numpy()
    frame.loc[label == "15:20", "volume"] = 0.0
    return frame


def serve_last_session_twice(rows, call_number):
    """The observed API behaviour: 21 Oct comes back as two contiguous, ascending, identical copies."""
    head = [r for r in rows if datetime.fromtimestamp(r[0], IST).day != 21]
    last = [r for r in rows if datetime.fromtimestamp(r[0], IST).day == 21]
    return head + last + [list(r) for r in last]


@pytest.fixture
def downloaded(sessions, tmp_path):
    api = FakeFyersApi(sessions, mutate=serve_last_session_twice)
    client, _ = make_client(api)
    result = download_nifty_5m(date(2026, 10, 15), date(2026, 10, 22), client=client, out_dir=tmp_path, now=RUN_TIME)
    (raw_file,) = result.raw_dir.glob("*.json")
    raw = json.loads(raw_file.read_text())["response"]["candles"]
    return api, result, raw


def test_the_simulated_response_has_the_confirmed_shape(downloaded):
    api, _, raw = downloaded
    assert len(api.calls) == 1  # a single request: the repeat is inside one response, not across windows
    assert len(raw) == 375
    dates = [(d, len(list(g))) for d, g in groupby(raw, key=lambda c: datetime.fromtimestamp(c[0], IST).date().isoformat())]
    assert dates == [("2026-10-15", 75), ("2026-10-16", 75), ("2026-10-19", 75), ("2026-10-21", 150)]
    first_copy, second_copy = raw[225:300], raw[300:375]
    assert first_copy == second_copy  # every timestamp and every OHLCV value identical
    for copy in (first_copy, second_copy):
        stamps = [c[0] for c in copy]
        assert stamps == sorted(set(stamps))  # ascending, no repeats within a copy


def test_a_session_returned_twice_is_collapsed_to_one_copy_and_every_value_is_preserved(sessions, downloaded):
    _, result, raw = downloaded
    meta, frame = result.metadata, result.frame

    # counts
    assert (meta["rows_received"], meta["rows"]) == (375, 300)
    assert (meta["duplicates_dropped"], meta["conflicting_duplicates"]) == (75, 0)
    assert meta["windows"] == 1 and meta["empty_windows"] == [] and meta["out_of_range_dropped"] == 0

    # session completeness: four sessions, each exactly the 75 labels 09:15..15:25, no gaps, no repeats
    assert frame["timestamp"].is_unique and frame["timestamp"].is_monotonic_increasing
    labels_by_day = {}
    for stamp in frame["timestamp"]:
        labels_by_day.setdefault(stamp.strftime("%Y-%m-%d"), []).append(stamp.strftime("%H:%M"))
    assert list(labels_by_day) == SESSION_DAYS
    assert all(labels == FULL_SESSION for labels in labels_by_day.values())
    assert meta["sessions"] == 4 and meta["incomplete_sessions"] == {}
    assert meta["weekdays_without_data"] == ["2026-10-20", "2026-10-22"]

    # retained values: exactly the original candles (no float tolerance) and exactly BOTH copies the API sent
    pd.testing.assert_frame_equal(frame, sessions, check_exact=True)
    assert frame_to_rows(frame) == frame_to_rows(sessions)
    retained_last = frame_to_rows(frame[frame["timestamp"].dt.day == 21])
    assert retained_last == raw[225:300] == raw[300:375]
    flat = frame[["open", "high", "low"]].eq(frame["close"], axis=0).all(axis=1)
    label = frame["timestamp"].dt.strftime("%H:%M")
    assert label[flat].value_counts().to_dict() == {"15:15": 4, "15:20": 4}  # flat candles are data, not duplicates
    assert label[frame["volume"] == 0].value_counts().to_dict() == {"15:20": 4}

    # outputs: the CSV holds the 300 retained rows; the raw file still holds everything the API sent
    assert len(result.csv_path.read_text().splitlines()) == 301
    assert raw == serve_last_session_twice(frame_to_rows(sessions), 1)
    assert len(raw) == 375
