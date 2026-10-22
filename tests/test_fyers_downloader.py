import json
import logging
import subprocess
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from nifty_signal_engine.fyers import (
    MAX_WINDOW_DAYS,
    FyersApiError,
    FyersAuthError,
    NoDataError,
    download_nifty_5m,
    split_date_range,
)
from nifty_signal_engine.schema import IST
from nifty_signal_engine.synthetic import generate_synthetic_candles
from nifty_signal_engine.validation import CandleValidationError

from fyers_helpers import (
    FAKE_APP_ID,
    FAKE_TOKEN,
    NOW,
    FakeFyersApi,
    ScriptedTransport,
    json_response,
    make_client,
    ok_payload,
    query,
)

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def candles():
    return generate_synthetic_candles(130, start=date(2024, 1, 1), seed=5)  # ~6 months, 9750 candles


def run(api, tmp_path, start=date(2024, 1, 1), end=date(2024, 6, 30), **kwargs):
    client, sleeps = make_client(api)
    kwargs.setdefault("now", NOW)
    return download_nifty_5m(start, end, client=client, out_dir=tmp_path / "out", **kwargs), sleeps


# --- date windows ------------------------------------------------------------------------------

def test_split_single_day_and_exact_window():
    assert split_date_range(date(2024, 1, 2), date(2024, 1, 2)) == [(date(2024, 1, 2), date(2024, 1, 2))]
    assert split_date_range(date(2024, 1, 1), date(2024, 3, 30), 90) == [(date(2024, 1, 1), date(2024, 3, 30))]  # 31+29+30 days
    assert len(split_date_range(date(2024, 1, 1), date(2024, 3, 31), 90)) == 2


@pytest.mark.parametrize("start, end, window", [
    (date(2024, 1, 1), date(2024, 12, 31), 90),
    (date(2023, 12, 20), date(2024, 3, 5), 7),
    (date(2020, 2, 1), date(2024, 3, 1), MAX_WINDOW_DAYS),  # leap years
])
def test_split_is_contiguous_non_overlapping_and_within_limit(start, end, window):
    windows = split_date_range(start, end, window)
    assert windows[0][0] == start and windows[-1][1] == end
    for (a_start, a_end), (b_start, _) in zip(windows, windows[1:]):
        assert b_start == a_end + timedelta(days=1)
    assert all((e - s).days + 1 <= window <= MAX_WINDOW_DAYS for s, e in windows)


@pytest.mark.parametrize("window", [0, -1, MAX_WINDOW_DAYS + 1])
def test_split_rejects_non_compliant_window(window):
    with pytest.raises(ValueError, match="window_days"):
        split_date_range(date(2024, 1, 1), date(2024, 2, 1), window)


def test_split_rejects_reversed_range():
    with pytest.raises(ValueError, match="after end"):
        split_date_range(date(2024, 2, 1), date(2024, 1, 1))


# --- happy path --------------------------------------------------------------------------------

def test_end_to_end_download_is_compliant_and_round_trips(candles, tmp_path):
    api = FakeFyersApi(candles)
    result, _ = run(api, tmp_path)

    windows = split_date_range(date(2024, 1, 1), date(2024, 6, 30), 90)
    assert len(api.calls) == len(windows) == 3
    for _, headers in api.calls:
        assert headers["Authorization"] == f"{FAKE_APP_ID}:{FAKE_TOKEN}"
    spans = [(int(query(u)["range_to"]) - int(query(u)["range_from"])) / 86400 for u, _ in api.calls]
    assert all(span <= MAX_WINDOW_DAYS for span in spans)

    pd.testing.assert_frame_equal(result.frame, candles)
    assert str(result.frame["timestamp"].dt.tz) == "Asia/Kolkata"
    assert result.csv_path.name == "NSE_NIFTY50-INDEX_5m_2024-01-01_2024-06-30.csv"
    assert result.metadata["rows"] == len(candles) and result.metadata["windows"] == 3
    assert len(list((tmp_path / "out" / "raw").glob("*.json"))) == 3


def test_epoch_seconds_are_converted_to_kolkata(tmp_path):
    assert int(datetime(2024, 1, 2, 9, 15, tzinfo=IST).timestamp()) == 1704167100  # 03:45 UTC
    rows = [[1704167100 + 300 * i, 100 + i, 101 + i, 99 + i, 100.5 + i, 0] for i in range(75)]
    result, _ = run(ScriptedTransport(json_response(ok_payload(rows))), tmp_path,
                    start=date(2024, 1, 2), end=date(2024, 1, 2))
    first, last = result.frame["timestamp"].iloc[[0, -1]]
    assert first == pd.Timestamp("2024-01-02 09:15", tz=IST)
    assert last == pd.Timestamp("2024-01-02 15:25", tz=IST)
    assert result.csv_path.read_text().splitlines()[1].startswith("2024-01-02T09:15:00+05:30,")




def test_timestamps_shifted_by_the_utc_offset_are_rejected_not_written(tmp_path):
    # If FYERS epochs were "IST-as-UTC", labels would land 5.5h late; validation must catch it.
    shifted = [[1704167100 + 19800 + 300 * i, 100, 101, 99, 100, 0] for i in range(75)]
    with pytest.raises(CandleValidationError, match="outside 09:15"):
        run(
            ScriptedTransport(json_response(ok_payload(shifted))),
            tmp_path,
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
        )
    assert not list((tmp_path / "out").glob("*.csv"))
    assert list((tmp_path / "out" / "raw").glob("*.json"))


def test_pre_session_candles_are_excluded_and_counted(tmp_path):
    start = int(datetime(2024, 1, 2, 9, 5, tzinfo=IST).timestamp())
    rows = [
        [start + 300 * i, 100, 101, 99, 100, 0]
        for i in range(77)
    ]

    result, _ = run(
        ScriptedTransport(json_response(ok_payload(rows))),
        tmp_path,
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )

    labels = result.frame["timestamp"].dt.strftime("%H:%M").tolist()
    assert len(labels) == 75
    assert labels[0] == "09:15"
    assert labels[-1] == "15:25"
    assert result.metadata["pre_session_candles_dropped"] == 2
    assert list((tmp_path / "out" / "raw").glob("*.json"))


# --- data quality ------------------------------------------------------------------------------

def test_identical_and_conflicting_duplicates_are_handled(candles, tmp_path):
    day1 = candles.iloc[:75]

    def add_duplicates(rows, call_number):
        rows = [list(r) for r in rows]
        rows.append(list(rows[0]))  # identical duplicate
        conflict = list(rows[1])
        conflict[4] += 5.0  # same timestamp, different close
        rows.append(conflict)
        return rows

    result, _ = run(FakeFyersApi(day1, mutate=add_duplicates), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert len(result.frame) == 75
    assert result.metadata["duplicates_dropped"] == 2 and result.metadata["conflicting_duplicates"] == 1
    pd.testing.assert_series_equal(result.frame["close"], day1["close"].reset_index(drop=True))  # first kept


def test_unsorted_response_is_sorted(candles, tmp_path):
    api = FakeFyersApi(candles.iloc[:75], mutate=lambda rows, n: rows[::-1])
    result, _ = run(api, tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert result.frame["timestamp"].is_monotonic_increasing


def test_candles_outside_requested_range_are_dropped(candles, tmp_path):
    api = FakeFyersApi(candles.iloc[:150], ignore_range=True)  # API ignores the range: 2 sessions come back
    result, _ = run(api, tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert len(result.frame) == 75
    assert result.metadata["out_of_range_dropped"] == 75


def test_incomplete_current_candle_is_dropped(candles, tmp_path):
    day = candles.iloc[:75]
    now = datetime(2024, 1, 1, 11, 2, tzinfo=IST)  # the 11:00 candle closes at 11:05
    result, _ = run(FakeFyersApi(day), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1), now=now)
    assert result.frame["timestamp"].iloc[-1] == pd.Timestamp("2024-01-01 10:55", tz=IST)
    assert len(result.frame) == 21  # 09:15 .. 10:55
    assert result.metadata["incomplete_candles_dropped"] == 54  # 11:00 .. 15:25 have not closed yet


def test_missing_sessions_and_partial_sessions_are_reported(candles, tmp_path):
    data = candles.iloc[:225].copy()  # Mon-Wed
    data = data[data["timestamp"].dt.day != 2]  # drop Tuesday entirely
    data = data.drop(index=data.index[-1])  # Wednesday ends at 15:20
    result, _ = run(FakeFyersApi(data), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 3))
    assert result.metadata["weekdays_without_data"] == ["2024-01-02"]
    assert result.metadata["incomplete_sessions"] == {"2024-01-03": 74}


def test_empty_window_is_recorded_but_not_fatal(candles, tmp_path):
    only_first_window = candles[candles["timestamp"] < pd.Timestamp("2024-03-29", tz=IST)]
    result, _ = run(FakeFyersApi(only_first_window), tmp_path)
    assert len(result.metadata["empty_windows"]) >= 1 and result.metadata["rows"] == len(only_first_window)


def test_no_data_anywhere_raises_and_keeps_raw(tmp_path):
    empty = json_response({"s": "no_data", "code": 200, "message": "", "candles": []})
    with pytest.raises(NoDataError, match="no candles"):
        run(ScriptedTransport(empty), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 5))
    assert list((tmp_path / "out" / "raw").glob("*.json"))
    assert not list((tmp_path / "out").glob("*.csv"))


@pytest.mark.parametrize("rows, match", [
    ([[1704167100, 1, 2]], "malformed candle"),
    ([[1704167100, 1, 2, 3, "x", 0]], "non-numeric"),
    ([[1704167100, 1, 2, 3, float("nan"), 0]], "non-numeric or non-finite"),
    ([[1704167100000, 1, 2, 3, 4, 0]], "epoch seconds"),
    (["not-a-row"], "malformed candle"),
])
def test_malformed_candles_raise_api_error(rows, match, tmp_path):
    payload = {"s": "ok", "code": 200, "message": "", "candles": rows}
    body = json.dumps(payload, allow_nan=True)
    from nifty_signal_engine.fyers import HttpResponse
    with pytest.raises(FyersApiError, match=match):
        run(ScriptedTransport(HttpResponse(200, {}, body.encode())), tmp_path,
            start=date(2024, 1, 2), end=date(2024, 1, 2))


def test_off_hours_candles_need_explicit_opt_in(tmp_path):
    rows = [[int(datetime(2024, 11, 1, 18, 0, tzinfo=IST).timestamp()) + 300 * i, 100, 101, 99, 100, 0] for i in range(3)]
    transport = ScriptedTransport(json_response(ok_payload(rows)))
    with pytest.raises(CandleValidationError):
        run(transport, tmp_path / "a", start=date(2024, 11, 1), end=date(2024, 11, 1))
    result, _ = run(transport, tmp_path / "b", start=date(2024, 11, 1), end=date(2024, 11, 1), allow_off_hours=True)
    assert len(result.frame) == 3


def test_future_start_is_rejected_and_future_end_is_clamped(candles, tmp_path):
    client, _ = make_client(FakeFyersApi(candles))
    with pytest.raises(ValueError, match="future"):
        download_nifty_5m(date(2025, 1, 1), date(2025, 2, 1), client=client, out_dir=tmp_path, now=NOW)
    result, _ = run(FakeFyersApi(candles.iloc[:75]), tmp_path, start=date(2024, 1, 1), end=date(2030, 1, 1))
    assert result.metadata["requested_end"] == "2024-12-31"


# --- failures and secrecy ----------------------------------------------------------------------

def test_auth_failure_midway_writes_no_csv_but_keeps_earlier_raw(candles, tmp_path):
    good = FakeFyersApi(candles)
    calls = []

    def flaky(url, headers, timeout):
        calls.append(url)
        if len(calls) == 2:
            return json_response({"s": "error", "code": -16, "message": "token invalid"})
        return good(url, headers, timeout)

    with pytest.raises(FyersAuthError):
        run(flaky, tmp_path)
    assert len(list((tmp_path / "out" / "raw").glob("*.json"))) == 1
    assert not list((tmp_path / "out").glob("*.csv"))


def test_no_credentials_in_any_output_file_or_log(candles, tmp_path, caplog):
    echoing = FakeFyersApi(candles.iloc[:75])

    def transport(url, headers, timeout):
        response = echoing(url, headers, timeout)
        body = json.loads(response.body)
        body["message"] = f"echo {FAKE_TOKEN} {FAKE_APP_ID}"  # worst case: API echoes secrets back
        return json_response(body)

    with caplog.at_level(logging.DEBUG):
        run(transport, tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert FAKE_TOKEN not in caplog.text and FAKE_APP_ID not in caplog.text
    files = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert len(files) >= 3  # csv, meta, raw
    for path in files:
        text = path.read_text()
        assert FAKE_TOKEN not in text and FAKE_APP_ID not in text, path


def test_metadata_flags_index_volume_as_untrusted(candles, tmp_path):
    result, _ = run(FakeFyersApi(candles.iloc[:75]), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1))
    meta = json.loads(result.meta_path.read_text())
    assert meta["volume_source"] == "index_reported"
    assert "must not be assumed to be genuine traded volume" in meta["volume_warning"]
    assert meta["symbol"] == "NSE:NIFTY50-INDEX" and meta["resolution_minutes"] == 5


def test_save_raw_can_be_disabled(candles, tmp_path):
    result, _ = run(FakeFyersApi(candles.iloc[:75]), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 1),
                    save_raw=False)
    assert result.raw_dir is None and not (tmp_path / "out" / "raw").exists()


def test_output_loads_with_existing_loader_and_engine_refuses_index_volume(candles, tmp_path):
    from nifty_signal_engine.data_loader import load_candles_csv
    from nifty_signal_engine.features import UnsupportedVolumeSourceError, relative_volume
    from nifty_signal_engine.schema import VolumeSource
    result, _ = run(FakeFyersApi(candles.iloc[:150]), tmp_path, start=date(2024, 1, 1), end=date(2024, 1, 2))
    loaded = load_candles_csv(result.csv_path, require_volume=True)
    with pytest.raises(UnsupportedVolumeSourceError):
        relative_volume(loaded, source=VolumeSource.INDEX_REPORTED)


# --- git hygiene -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [".env", "data/local/fyers/NSE_NIFTY50-INDEX_5m_a_b.csv",
                                  "data/local/fyers/NSE_NIFTY50-INDEX_5m_a_b.meta.json",
                                  "data/local/fyers/raw/x.json"])
def test_secret_and_download_paths_are_git_ignored(path):
    try:
        outcome = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True)
    except FileNotFoundError:
        pytest.skip("git not installed")
    if outcome.returncode == 128:
        pytest.skip("not a git checkout")
    assert outcome.returncode == 0, f"{path} is not ignored by .gitignore"
