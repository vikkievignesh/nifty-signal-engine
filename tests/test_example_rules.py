from nifty_signal_engine.regime import MarketRegime
from nifty_signal_engine.signals import Direction, SetupState, SignalStateMachine, run_signals
from nifty_signal_engine.signals.example_rules import (
    CprCrossBreakout,
    RetestExtremeConfirmation,
    example_ruleset,
)
from nifty_signal_engine.signals.models import Setup
from nifty_signal_engine.signals.runner import iter_contexts

from conftest import make_candles


def with_levels(df, top=100.0, bottom=99.0, rvol=float("nan")):
    df = df.copy()
    df["cpr_top"], df["cpr_bottom"], df["rvol"] = top, bottom, rvol
    df["regime"] = MarketRegime.MIXED.value
    return df


def test_breakout_retest_confirm_sequence():
    # 0: below; 1: cross above 100; 2: dip to 100.0x and close above (retest); 3: close above retest high
    df = make_candles("2024-01-02", [99.5, 100.6, 100.3, 101.5, 101.6], spread=0.05)
    df.loc[2, "low"] = 100.01
    result = run_signals(with_levels(df), SignalStateMachine(example_ruleset()))
    states = [t.to_state for t in result.transitions]
    assert states == [SetupState.BREAKOUT_DETECTED, SetupState.RETEST, SetupState.CONFIRMED]
    (alert,) = result.alerts
    assert alert.direction is Direction.BREAKOUT and alert.level_name == "cpr_top"


def test_breakdown_invalidated_by_close_back_above():
    df = make_candles("2024-01-02", [99.5, 98.5, 99.6], spread=0.05)
    result = run_signals(with_levels(df), SignalStateMachine(example_ruleset()))
    assert [t.to_state for t in result.transitions] == [SetupState.BREAKOUT_DETECTED, SetupState.INVALIDATED]
    assert result.transitions[0].setup_id.endswith("BREAKDOWN-1")
    assert not result.alerts


def test_nan_levels_never_break_out():
    df = with_levels(make_candles("2024-01-02", [99, 101, 103]), top=float("nan"), bottom=float("nan"))
    assert all(CprCrossBreakout().detect(c) is None for c in iter_contexts(df))


def test_regime_filter():
    df = with_levels(make_candles("2024-01-02", [99.5, 100.6]))
    ctx = list(iter_contexts(df))[1]
    assert CprCrossBreakout().detect(ctx) is not None
    assert CprCrossBreakout(allowed_regimes={MarketRegime.TREND_UP}).detect(ctx) is None


def test_rvol_requirement_with_missing_rvol_does_not_confirm():
    df = make_candles("2024-01-02", [99.5, 100.6, 100.3, 101.5], spread=0.05)
    df.loc[2, "low"] = 100.01
    ctxs = list(iter_contexts(with_levels(df)))
    setup = Setup("s", Direction.BREAKOUT, 100.0, "cpr_top", ctxs[0].session_date, ctxs[1].timestamp, 1,
                  SetupState.RETEST, retest=ctxs[2])
    assert RetestExtremeConfirmation().is_confirmed(ctxs[3], setup)
    assert not RetestExtremeConfirmation(min_rvol=1.5).is_confirmed(ctxs[3], setup)
