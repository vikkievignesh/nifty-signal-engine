"""Pluggable rule interfaces for the setup state machine.

Implementations must be pure functions of the supplied context/setup (no access to
future candles). See :mod:`.example_rules` for unvalidated placeholder rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import CandleContext, Direction, Setup


@dataclass(frozen=True)
class BreakoutCandidate:
    direction: Direction
    level: float
    level_name: str
    reason: str = ""


class BreakoutDetector(Protocol):
    def detect(self, ctx: CandleContext) -> BreakoutCandidate | None:
        """Return a candidate if this candle breaks a reference level."""


class RetestDetector(Protocol):
    def is_retest(self, ctx: CandleContext, setup: Setup) -> bool:
        """True if this candle pulls back to / retests the broken level."""


class ConfirmationRule(Protocol):
    def is_confirmed(self, ctx: CandleContext, setup: Setup) -> bool:
        """True if this candle confirms the setup after a retest."""


class InvalidationRule(Protocol):
    def invalidation_reason(self, ctx: CandleContext, setup: Setup) -> str | None:
        """A reason string if the setup is invalidated by this candle, else None."""


@dataclass(frozen=True)
class RuleSet:
    breakout: BreakoutDetector
    retest: RetestDetector
    confirmation: ConfirmationRule
    invalidation: InvalidationRule
