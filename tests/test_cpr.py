import math

import pandas as pd
import pytest

from nifty_signal_engine.features.cpr import compute_cpr, daily_hlc

from conftest import make_candles


def two_sessions():
    d1 = make_candles("2024-01-02", [100, 110, 95, 105], spread=0.0)  # H=110 L=95 C=105
    d2 = make_candles("2024-01-03", [106, 107, 108], spread=0.0)
    return pd.concat([d1, d2], ignore_index=True)


def test_daily_hlc():
    daily = daily_hlc(two_sessions())
    first = daily.iloc[0]
    assert (first["high"], first["low"], first["close"], first["n_candles"]) == (110, 95, 105, 4)


def test_cpr_uses_previous_session_only():
    df = two_sessions()
    cpr = compute_cpr(df, min_session_candles=3)
    assert cpr.iloc[:4].isna().all().all()  # first session: no previous session
    pivot = (110 + 95 + 105) / 3
    bc = (110 + 95) / 2
    tc = 2 * pivot - bc
    row = cpr.iloc[4]
    assert row["cpr_pivot"] == pytest.approx(pivot)
    assert row["cpr_bc"] == pytest.approx(bc)
    assert row["cpr_tc"] == pytest.approx(tc)
    assert row["cpr_top"] == pytest.approx(max(tc, bc))
    assert row["cpr_bottom"] == pytest.approx(min(tc, bc))
    assert row["cpr_width_pct"] == pytest.approx(abs(tc - bc) / pivot * 100)
    assert cpr.iloc[4:].nunique().eq(1).all()  # constant within the session


def test_top_bottom_ordered_when_close_is_weak():
    d1 = make_candles("2024-01-02", [100, 110, 95, 96], spread=0.0)  # close near low -> TC < BC
    d2 = make_candles("2024-01-03", [97], spread=0.0)
    cpr = compute_cpr(pd.concat([d1, d2], ignore_index=True), min_session_candles=1).iloc[-1]
    assert cpr["cpr_tc"] < cpr["cpr_bc"]
    assert cpr["cpr_top"] == cpr["cpr_bc"] and cpr["cpr_bottom"] == cpr["cpr_tc"]


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
    daily = pd.DataFrame({"high": [120.0], "low": [90.0], "close": [100.0]},
                         index=[df["timestamp"].iloc[0].normalize()])
    cpr = compute_cpr(df, daily=daily)
    assert cpr.iloc[-1]["cpr_pivot"] == pytest.approx((120 + 90 + 100) / 3)
    assert math.isnan(cpr.iloc[0]["cpr_pivot"])
