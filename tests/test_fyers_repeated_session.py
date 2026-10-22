"""Regression: one FYERS history response contained a whole session twice.

Confirmed pattern from a real run (a single request, one response of 375 rows): 15 Oct (75 rows), 16 Oct (75),
19 Oct (75) and 21 Oct (150) -- the last being two contiguous, ascending copies of the same 75-candle session with
identical OHLCV. 20 and 22 Oct have no data. The downloader must keep exactly one copy, report the counts, leave
every retained value untouched, and keep the raw response un-deduplicated for debugging.

All data below is synthetic (seeded, network-free); only the shape of the real response is reproduced.
"""

import json
import logging
from datetime import date, datetime
from itertools import groupby

import pandas as pd
import pytest

from nifty_signal_engine.fyers import download_nifty_5m
from nifty_signal_engine.schema import IST
from nifty_signal_engine.synthetic import generate_synthetic_candles

from fyers_helpers import FAKE_APP_ID, FAKE_TOKEN, FakeFyersApi, frame_to_rows, make_client

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
    assert meta["duplicates_by_date"] == {"2026-10-21": {"dropped": 75, "conflicting": 0, "full_session_repeat": True}}

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


# --- classification of repeated rows: warning-only, the first-occurrence dedupe itself is unchanged ------------

DOWNLOADER_LOGGER = "nifty_signal_engine.fyers.downloader"
D21, D19 = "2026-10-21", "2026-10-19"
VOLUME = 5  # column index of volume in [timestamp, open, high, low, close, volume]


def on_day(rows, day):
    return [r for r in rows if datetime.fromtimestamp(r[0], IST).day == day]


def off_days(rows, *days):
    return [r for r in rows if datetime.fromtimestamp(r[0], IST).day not in days]


def copy(rows):
    return [list(r) for r in rows]


def with_volume_changed(rows, index):
    rows = copy(rows)
    rows[index][VOLUME] += 1.0
    return rows


def entry(dropped, conflicting=0, full=False):
    return {"dropped": dropped, "conflicting": conflicting, "full_session_repeat": full}


@pytest.fixture(scope="module")
def short_sessions(sessions):
    """The same data, but 21 Oct stops at 14:10 (60 of its 75 candles)."""
    late = (sessions["timestamp"].dt.day == 21) & (sessions["timestamp"].dt.strftime("%H:%M") > "14:10")
    return sessions[~late].reset_index(drop=True)


def run_scenario(frame, build, tmp_path):
    api = FakeFyersApi(frame, mutate=lambda rows, call_number: build(rows))
    client, _ = make_client(api)
    result = download_nifty_5m(date(2026, 10, 15), date(2026, 10, 22), client=client, out_dir=tmp_path, now=RUN_TIME)
    (raw_file,) = result.raw_dir.glob("*.json")
    return result, json.loads(raw_file.read_text())["response"]["candles"]


# (id, how the response is built from the clean rows, uses the 60-candle 21 Oct, expected duplicates_by_date,
#  expected global conflicting count)
SCENARIOS = [
    ("no_duplicates", lambda rows: rows, False, {}, 0),
    ("full_contiguous",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + copy(on_day(rows, 21)), False, {D21: entry(75, full=True)}, 0),
    ("full_interleaved",
     lambda rows: off_days(rows, 21) + [x for r in on_day(rows, 21) for x in (r, list(r))], False,
     {D21: entry(75, full=True)}, 0),
    ("full_copy_first", lambda rows: copy(on_day(rows, 21)) + rows, False, {D21: entry(75, full=True)}, 0),
    ("full_reversed", lambda rows: rows + copy(on_day(rows, 21))[::-1], False, {D21: entry(75, full=True)}, 0),
    ("two_different_sessions_repeated",
     lambda rows: off_days(rows, 19, 21) + on_day(rows, 19) + copy(on_day(rows, 19)) + on_day(rows, 21)
     + copy(on_day(rows, 21)), False, {D19: entry(75, full=True), D21: entry(75, full=True)}, 0),
    ("full_repeat_plus_isolated_duplicates_on_another_date",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + copy(on_day(rows, 21)) + copy(on_day(rows, 19)[:3]), False,
     {D19: entry(3), D21: entry(75, full=True)}, 0),
    ("full_repeat_plus_conflicting_duplicate_on_another_date",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + copy(on_day(rows, 21))
     + [with_volume_changed(on_day(rows, 19), 4)[4]], False,
     {D19: entry(1, conflicting=1), D21: entry(75, full=True)}, 1),
    ("isolated_identical_duplicates", lambda rows: rows + copy(on_day(rows, 21)[:3]), False, {D21: entry(3)}, 0),
    ("one_conflicting_duplicate",
     lambda rows: rows + [with_volume_changed(on_day(rows, 21), 7)[7]], False, {D21: entry(1, conflicting=1)}, 1),
    ("full_repeat_but_one_value_differs",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + with_volume_changed(on_day(rows, 21), 7), False,
     {D21: entry(75, conflicting=1)}, 1),
    ("partial_repeat_40_of_75", lambda rows: rows + copy(on_day(rows, 21)[:40]), False, {D21: entry(40)}, 0),
    ("partial_repeat_74_of_75", lambda rows: rows + copy(on_day(rows, 21)[:74]), False, {D21: entry(74)}, 0),
    ("one_timestamp_three_times", lambda rows: rows + copy(on_day(rows, 21)[:1]) * 2, False, {D21: entry(2)}, 0),
    ("triple_copy",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + copy(on_day(rows, 21)) * 2, False, {D21: entry(150)}, 0),
    ("incomplete_session_repeated",
     lambda rows: off_days(rows, 21) + on_day(rows, 21) + copy(on_day(rows, 21)), True, {D21: entry(60)}, 0),
]


@pytest.mark.parametrize("name, build, short, expected, conflicting", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_repeated_rows_are_classified_per_date_and_only_a_full_session_gets_the_extra_warning(
        name, build, short, expected, conflicting, sessions, short_sessions, tmp_path, caplog):
    base = short_sessions if short else sessions
    with caplog.at_level(logging.INFO):
        result, raw = run_scenario(base, build, tmp_path)
    meta = result.metadata
    dropped = sum(e["dropped"] for e in expected.values())

    # per-date classification, reconciled exactly with the global totals
    assert meta["duplicates_by_date"] == expected
    assert sum(e["dropped"] for e in meta["duplicates_by_date"].values()) == meta["duplicates_dropped"] == dropped
    assert sum(e["conflicting"] for e in meta["duplicates_by_date"].values()) == meta["conflicting_duplicates"]
    assert meta["conflicting_duplicates"] == conflicting
    assert (meta["rows_received"], meta["rows"]) == (len(base) + dropped, len(base))
    if short:
        assert meta["incomplete_sessions"] == {D21: 60}

    # unchanged behaviour: the first occurrence is kept and every retained value is exact
    pd.testing.assert_frame_equal(result.frame, base, check_exact=True)
    # the raw response is exactly what the API served, repeats included
    assert raw == build(frame_to_rows(base)) and len(raw) == meta["rows_received"]

    # logs: the generic warning is unchanged; the full-session warning appears only for a valid full-session repeat
    messages = [r.getMessage() for r in caplog.records if r.name == DOWNLOADER_LOGGER and r.levelno >= logging.WARNING]
    generic = [m for m in messages if m.startswith("dropped ") and "duplicate candle(s)" in m]
    full_warnings = [m for m in messages if "full trading session" in m]
    assert generic == ([f"dropped {dropped} duplicate candle(s), {conflicting} with conflicting values (first kept)"]
                       if dropped else [])
    full_dates = [d for d, e in expected.items() if e["full_session_repeat"]]
    if full_dates:
        (message,) = full_warnings
        assert f"{len(full_dates)} full trading session(s) twice" in message
        assert all(d in message for d in full_dates)
        assert f"dropped {sum(expected[d]['dropped'] for d in full_dates)} duplicate candle(s)" in message
    else:
        assert full_warnings == []

    # nothing sensitive in the logs or metadata: no credentials and none of the session's prices
    text = caplog.text + json.dumps(meta, default=str)
    assert FAKE_TOKEN not in text and FAKE_APP_ID not in text
    prices = {repr(float(v)) for v in base[["open", "high", "low", "close"]].to_numpy().ravel()}
    assert not any(price in text for price in prices)


def test_the_full_session_warning_lists_at_most_ten_dates_but_the_metadata_lists_all(tmp_path, caplog):
    frame = generate_synthetic_candles(12, start=date(2026, 9, 1), seed=4)  # 12 complete weekday sessions
    days = sorted({stamp.strftime("%Y-%m-%d") for stamp in frame["timestamp"]})
    api = FakeFyersApi(frame, mutate=lambda rows, call_number: rows + copy(rows))  # every session returned twice
    client, _ = make_client(api)
    with caplog.at_level(logging.WARNING):
        result = download_nifty_5m(date(2026, 9, 1), date.fromisoformat(days[-1]), client=client, out_dir=tmp_path,
                                   now=RUN_TIME)
    assert sorted(result.metadata["duplicates_by_date"]) == days
    assert all(e == entry(75, full=True) for e in result.metadata["duplicates_by_date"].values())
    (message,) = [r.getMessage() for r in caplog.records if "full trading session" in r.getMessage()]
    assert "12 full trading session(s) twice" in message and "and 2 more" in message
    assert all(d in message for d in days[:10]) and not any(d in message for d in days[10:])
    assert "dropped 900 duplicate candle(s)" in message
