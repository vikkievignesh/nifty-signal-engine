from dataclasses import dataclass, field
from types import MappingProxyType

import pandas as pd
import pytest

from nifty_signal_engine.regime import MarketRegime
from nifty_signal_engine.schema import IST
from nifty_signal_engine.signals import (
    BreakoutCandidate,
    CandleContext,
    Direction,
    OutOfOrderCandleError,
    RuleSet,
    SetupState,
    SignalStateMachine,
    StateMachineConfig,
)

S = SetupState
DAY1 = pd.Timestamp("2024-01-02 09:15", tz=IST)
DAY2 = pd.Timestamp("2024-01-03 09:15", tz=IST)


def ctx(i, start=DAY1, **feats):
    return CandleContext(index=i, timestamp=start + pd.Timedelta(minutes=5 * i), open=100, high=101, low=99,
                         close=100, volume=None, regime=MarketRegime.MIXED, features=MappingProxyType(feats))


@dataclass
class Scripted:
    """Rules that fire on pre-set candle indices, to test the machine in isolation."""
    breakouts: set = field(default_factory=set)
    retests: set = field(default_factory=set)
    confirms: set = field(default_factory=set)
    invalids: set = field(default_factory=set)

    def detect(self, c):
        return BreakoutCandidate(Direction.BREAKOUT, 100.0, "lvl") if c.index in self.breakouts else None

    def is_retest(self, c, setup):
        return c.index in self.retests

    def is_confirmed(self, c, setup):
        return c.index in self.confirms

    def invalidation_reason(self, c, setup):
        return "invalid" if c.index in self.invalids else None


def machine(script, **cfg):
    return SignalStateMachine(RuleSet(script, script, script, script), StateMachineConfig(**cfg))


def run(m, indices, start=DAY1):
    transitions, alerts = [], []
    for i in indices:
        r = m.step(ctx(i, start))
        transitions += [(i, t.from_state, t.to_state) for t in r.transitions]
        alerts += r.alerts
    return transitions, alerts


def test_full_path_to_confirmation_emits_one_alert():
    m = machine(Scripted(breakouts={1}, retests={1, 3}, confirms={3, 5}), alert_ttl_bars=2)
    transitions, alerts = run(m, range(7))
    assert transitions == [
        (1, S.IDLE, S.BREAKOUT_DETECTED),
        (3, S.BREAKOUT_DETECTED, S.RETEST),  # retest can't happen on the breakout candle
        (5, S.RETEST, S.CONFIRMED),  # confirmation can't happen on the retest candle
    ]
    assert m.state is S.IDLE
    (alert,) = alerts
    assert alert.direction is Direction.BREAKOUT and alert.validated is False
    assert alert.candle_timestamp == DAY1 + pd.Timedelta(minutes=25)
    assert alert.available_at == DAY1 + pd.Timedelta(minutes=30)  # candle close, not open
    assert alert.expires_at == alert.available_at + pd.Timedelta(minutes=10)
    assert alert.is_active(alert.available_at)
    assert not alert.is_active(alert.candle_timestamp)
    assert not alert.is_active(alert.expires_at)


def test_invalidation():
    m = machine(Scripted(breakouts={0}, invalids={2}))
    transitions, alerts = run(m, range(4))
    assert transitions[-1] == (2, S.BREAKOUT_DETECTED, S.INVALIDATED)
    assert not alerts and m.state is S.IDLE


def test_invalidation_from_retest_state():
    m = machine(Scripted(breakouts={0}, retests={1}, invalids={2}))
    transitions, _ = run(m, range(3))
    assert transitions[-1] == (2, S.RETEST, S.INVALIDATED)


def test_retest_timeout_expires():
    m = machine(Scripted(breakouts={0}), max_bars_to_retest=3)
    transitions, _ = run(m, range(5))
    assert transitions == [(0, S.IDLE, S.BREAKOUT_DETECTED), (3, S.BREAKOUT_DETECTED, S.EXPIRED)]


def test_retest_on_last_allowed_bar_counts():
    m = machine(Scripted(breakouts={0}, retests={3}), max_bars_to_retest=3)
    transitions, _ = run(m, range(4))
    assert transitions[-1] == (3, S.BREAKOUT_DETECTED, S.RETEST)


def test_confirmation_timeout_expires():
    m = machine(Scripted(breakouts={0}, retests={1}), max_bars_to_confirm=2)
    transitions, _ = run(m, range(5))
    assert transitions[-1] == (3, S.RETEST, S.EXPIRED)


def test_no_new_breakout_on_terminal_candle():
    m = machine(Scripted(breakouts={0, 2, 3}, invalids={2}))
    transitions, _ = run(m, range(4))
    assert transitions == [(0, S.IDLE, S.BREAKOUT_DETECTED), (2, S.BREAKOUT_DETECTED, S.INVALIDATED),
                           (3, S.IDLE, S.BREAKOUT_DETECTED)]


def test_session_end_expires_and_allows_new_breakout_same_candle():
    m = machine(Scripted(breakouts={0}))
    run(m, [0, 1])
    r = m.step(ctx(0, DAY2))
    assert [(t.from_state, t.to_state, t.reason) for t in r.transitions] == [
        (S.BREAKOUT_DETECTED, S.EXPIRED, "session_end"),
        (S.IDLE, S.BREAKOUT_DETECTED, "breakout of lvl"),
    ]
    assert m.active_setup.session_date == DAY2.normalize()


def test_out_of_order_candles_rejected():
    m = machine(Scripted())
    m.step(ctx(2))
    with pytest.raises(OutOfOrderCandleError):
        m.step(ctx(2))
    with pytest.raises(OutOfOrderCandleError):
        m.step(ctx(1))


def test_invalid_config():
    with pytest.raises(ValueError):
        StateMachineConfig(alert_ttl_bars=0)
