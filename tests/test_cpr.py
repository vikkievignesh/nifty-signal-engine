import math

import pandas as pd
import pytest

from conftest import make_candles
from nifty_signal_engine.features.cpr import (
    compute_cpr,
    compute_monthly_cpr,
    compute_weekly_cpr,
    daily_hlc,
)


def two_sessions():
    d1 = make_candles(
        "2024-01-02", [100, 110, 95, 105], spread=0.0
    )
    d2 = make_candles(
        "2024-01-03", [106, 107, 108], spread=0.0
    )
    return pd.concat([d1, d2], ignore_index=True)


def test_daily_hlc():
    daily = daily_hlc(two_sessions())
    first = daily.iloc[0]

    assert first["high"] == 110
    assert first["low"] == 95
    assert first["close"] == 105
    assert first["n_candles"] == 4


def test_cpr_uses_previous_session_only():
    df = two_sessions()
    cpr = compute_cpr(df, min_session_candles=3)

    assert cpr.iloc[:4].isna().all().all()

    pivot = (110 + 95 + 105) / 3
    bc = (110 + 95) / 2
    tc = 2 * pivot - bc

    row = cpr.iloc[4]

    assert row["cpr_pivot"] == pytest.approx(pivot)
    assert row["cpr_bc"] == pytest.approx(bc)
    assert row["cpr_tc"] == pytest.approx(tc)
    assert row["cpr_top"] == pytest.approx(max(tc, bc))
    assert row["cpr_bottom"] == pytest.approx(min(tc, bc))
    assert row["cpr_width_pct"] == pytest.approx(
        abs(tc - bc) / pivot * 100
    )
    assert cpr.iloc[4:].nunique().eq(1).all()


def test_top_bottom_ordered_when_close_is_weak():
    d1 = make_candles(
        "2024-01-02", [100, 110, 95, 96], spread=0.0
    )
    d2 = make_candles("2024-01-03", [97], spread=0.0)

    df = pd.concat([d1, d2], ignore_index=True)
    row = compute_cpr(df, min_session_candles=1).iloc[-1]

    assert row["cpr_tc"] < row["cpr_bc"]
    assert row["cpr_top"] == row["cpr_bc"]
    assert row["cpr_bottom"] == row["cpr_tc"]


def test_incomplete_previous_session_gives_nan():
    cpr = compute_cpr(two_sessions(), min_session_candles=75)

    assert cpr.isna().all().all()


def test_current_session_does_not_affect_its_own_cpr():
    df = two_sessions()
    base = compute_cpr(df, min_session_candles=3)

    shocked = df.copy()
    shocked.loc[5, ["high", "close"]] = [500.0, 500.0]

    after = compute_cpr(shocked, min_session_candles=3)

    pd.testing.assert_frame_equal(base, after)


def test_daily_override():
    df = two_sessions()

    daily = pd.DataFrame(
        {
            "high": [120.0],
            "low": [90.0],
            "close": [100.0],
        },
        index=[df["timestamp"].iloc[0].normalize()],
    )

    cpr = compute_cpr(df, daily=daily)

    assert cpr.iloc[-1]["cpr_pivot"] == pytest.approx(
        (120 + 90 + 100) / 3
    )
    assert math.isnan(cpr.iloc[0]["cpr_pivot"])


def test_weekly_cpr_uses_previous_calendar_week():
    days = [
        make_candles("2024-01-08", [100, 105], spread=1.0),
        make_candles("2024-01-09", [106, 108], spread=1.0),
        make_candles("2024-01-15", [110, 112], spread=1.0),
    ]
    df = pd.concat(days, ignore_index=True)

    cpr = compute_weekly_cpr(df, min_session_candles=2)

    pivot = (109 + 99 + 108) / 3
    bc = (109 + 99) / 2
    tc = 2 * pivot - bc

    row = cpr.iloc[4]

    assert row["cpr_pivot"] == pytest.approx(pivot)
    assert row["cpr_bc"] == pytest.approx(bc)
    assert row["cpr_tc"] == pytest.approx(tc)
    assert row["cpr_top"] == pytest.approx(max(bc, tc))
    assert row["cpr_bottom"] == pytest.approx(min(bc, tc))


def test_monthly_cpr_uses_previous_calendar_month():
    days = [
        make_candles("2024-01-30", [100, 105], spread=1.0),
        make_candles("2024-02-01", [110, 112], spread=1.0),
    ]
    df = pd.concat(days, ignore_index=True)

    cpr = compute_monthly_cpr(df, min_session_candles=2)

    pivot = (106 + 99 + 105) / 3
    bc = (106 + 99) / 2
    tc = 2 * pivot - bc

    row = cpr.iloc[2]

    assert row["cpr_pivot"] == pytest.approx(pivot)
    assert row["cpr_bc"] == pytest.approx(bc)
    assert row["cpr_tc"] == pytest.approx(tc)
    assert row["cpr_top"] == pytest.approx(max(bc, tc))
    assert row["cpr_bottom"] == pytest.approx(min(bc, tc))


def test_weekly_cpr_does_not_use_current_week_prices():
    df = pd.concat(
        [
            make_candles("2024-01-08", [100, 105], spread=1.0),
            make_candles("2024-01-15", [110, 112], spread=1.0),
        ],
        ignore_index=True,
    )

    before = compute_weekly_cpr(df, min_session_candles=2)

    changed = df.copy()
    changed.loc[2:, ["high", "low", "close"]] = [
        500.0, 499.0, 500.0
    ]

    after = compute_weekly_cpr(changed, min_session_candles=2)

    pd.testing.assert_frame_equal(before, after)


def test_weekly_cpr_is_nan_when_previous_observed_session_is_incomplete():
    previous_week = make_candles(
        "2024-01-08", [100, 105], spread=1.0
    )
    current_week = make_candles(
        "2024-01-15", [110, 112], spread=1.0
    )
    df = pd.concat(
        [previous_week, current_week],
        ignore_index=True,
    )

    # Default threshold is 75 candles, so the two-candle prior session
    # is incomplete and cannot produce weekly CPR.
    cpr = compute_weekly_cpr(df)

    assert cpr.iloc[-2:].isna().all().all()
