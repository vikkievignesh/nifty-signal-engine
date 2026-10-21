"""Breakout/breakdown → retest → confirmation state machine (alert-only).

    IDLE ──breakout──▶ BREAKOUT_DETECTED ──retest──▶ RETEST ──confirm──▶ CONFIRMED (alert)
                            │   │                     │   │
                            │   └─ no retest within ──┘   └─ no confirmation within
                            │      max_bars_to_retest        max_bars_to_confirm ─▶ EXPIRED
                            └─ invalidation rule (any non-terminal state) ─────────▶ INVALIDATED
    Session change ─▶ EXPIRED (when expire_at_session_end)

At most one transition happens per candle (plus the reset to IDLE after a
terminal state), so a breakout, retest and confirmation always occur on distinct
candles. Candles must be fed in strictly increasing time order; the machine never
sees future candles. Terminal setups are discarded and the machine returns to IDLE;
a new breakout is only searched on the same candle when the previous setup expired
because the session changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from ..schema import CANDLE_INTERVAL, SESSION_CLOSE
from .interfaces import RuleSet
from .models import TERMINAL_STATES, Alert, CandleContext, Setup, SetupState, StateTransition

logger = logging.getLogger(__name__)

SESSION_END_REASON = "session_end"


class OutOfOrderCandleError(ValueError):
    """Raised when candles are not fed in strictly increasing time order."""


@dataclass(frozen=True)
class StateMachineConfig:
    """Timing limits, in candles. Defaults are UNVALIDATED placeholders."""

    max_bars_to_retest: int = 6
    max_bars_to_confirm: int = 3
    alert_ttl_bars: int = 3
    expire_at_session_end: bool = True  # also caps alert expiry at the 15:30 close

    def __post_init__(self) -> None:
        for name in ("max_bars_to_retest", "max_bars_to_confirm", "alert_ttl_bars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass
class StepResult:
    transitions: list[StateTransition] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


class SignalStateMachine:
    def __init__(
        self,
        rules: RuleSet,
        config: StateMachineConfig = StateMachineConfig(),
        *,
        interval: timedelta = CANDLE_INTERVAL,
    ) -> None:
        self.rules = rules
        self.config = config
        self.interval = interval
        self._setup: Setup | None = None
        self._last_timestamp: pd.Timestamp | None = None
        self._counter = 0

    @property
    def state(self) -> SetupState:
        return self._setup.state if self._setup else SetupState.IDLE

    @property
    def active_setup(self) -> Setup | None:
        return self._setup

    def step(self, ctx: CandleContext) -> StepResult:
        """Process one completed candle."""
        if self._last_timestamp is not None and ctx.timestamp <= self._last_timestamp:
            raise OutOfOrderCandleError(f"candle {ctx.timestamp} is not after {self._last_timestamp}")
        self._last_timestamp = ctx.timestamp

        result = StepResult()
        if self._setup is not None:
            reason = self._advance(self._setup, ctx, result)
            if self._setup.state not in TERMINAL_STATES:
                return result
            self._setup = None
            if reason != SESSION_END_REASON:
                return result

        candidate = self.rules.breakout.detect(ctx)
        if candidate is not None:
            self._counter += 1
            setup = Setup(
                setup_id=f"{ctx.session_date:%Y%m%d}-{candidate.direction.value}-{self._counter}",
                direction=candidate.direction,
                level=candidate.level,
                level_name=candidate.level_name,
                session_date=ctx.session_date,
                detected_at=ctx.timestamp,
                detected_index=ctx.index,
            )
            self._setup = setup
            self._record(result, ctx, setup, SetupState.IDLE, SetupState.BREAKOUT_DETECTED,
                         candidate.reason or f"{candidate.direction.value.lower()} of {candidate.level_name}")
        return result

    def _advance(self, setup: Setup, ctx: CandleContext, result: StepResult) -> str | None:
        """Move ``setup`` forward by at most one state; return the transition reason."""
        cfg = self.config
        if cfg.expire_at_session_end and ctx.session_date != setup.session_date:
            return self._move(result, ctx, setup, SetupState.EXPIRED, SESSION_END_REASON)

        invalid = self.rules.invalidation.invalidation_reason(ctx, setup)
        if invalid:
            return self._move(result, ctx, setup, SetupState.INVALIDATED, invalid)

        if setup.state is SetupState.BREAKOUT_DETECTED:
            if self.rules.retest.is_retest(ctx, setup):
                setup.retest = ctx
                return self._move(result, ctx, setup, SetupState.RETEST, "retest of level")
            if ctx.index - setup.detected_index >= cfg.max_bars_to_retest:
                return self._move(result, ctx, setup, SetupState.EXPIRED,
                                  f"no retest within {cfg.max_bars_to_retest} candles")
        elif setup.state is SetupState.RETEST:
            assert setup.retest is not None
            if self.rules.confirmation.is_confirmed(ctx, setup):
                reason = "confirmation after retest"
                self._move(result, ctx, setup, SetupState.CONFIRMED, reason)
                result.alerts.append(self._make_alert(setup, ctx, reason))
                return reason
            if ctx.index - setup.retest.index >= cfg.max_bars_to_confirm:
                return self._move(result, ctx, setup, SetupState.EXPIRED,
                                  f"no confirmation within {cfg.max_bars_to_confirm} candles")
        return None

    def _move(self, result: StepResult, ctx: CandleContext, setup: Setup, to: SetupState, reason: str) -> str:
        frm = setup.state
        setup.state = to
        self._record(result, ctx, setup, frm, to, reason)
        return reason

    def _record(self, result: StepResult, ctx: CandleContext, setup: Setup,
                frm: SetupState, to: SetupState, reason: str) -> None:
        result.transitions.append(StateTransition(ctx.timestamp, setup.setup_id, frm, to, reason))
        logger.debug("%s %s: %s -> %s (%s)", ctx.timestamp, setup.setup_id, frm, to, reason)

    def _make_alert(self, setup: Setup, ctx: CandleContext, reason: str) -> Alert:
        expires_at = ctx.available_at + self.config.alert_ttl_bars * self.interval
        if self.config.expire_at_session_end:
            session_close = ctx.session_date + pd.Timedelta(hours=SESSION_CLOSE.hour, minutes=SESSION_CLOSE.minute)
            expires_at = max(ctx.available_at, min(expires_at, session_close))
        alert = Alert(
            alert_id=f"{setup.setup_id}-alert",
            setup_id=setup.setup_id,
            direction=setup.direction,
            level=setup.level,
            level_name=setup.level_name,
            candle_timestamp=ctx.timestamp,
            available_at=ctx.available_at,
            expires_at=expires_at,
            regime=ctx.regime,
            reason=reason,
        )
        logger.info("UNVALIDATED research alert %s %s @ %s (level %s=%.2f)",
                    alert.alert_id, alert.direction, alert.available_at, alert.level_name, alert.level)
        return alert
