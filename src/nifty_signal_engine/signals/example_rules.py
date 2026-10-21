"""EXAMPLE rules for exercising the state machine.

UNVALIDATED PLACEHOLDERS. These rules exist only so the scaffold can run end to
end on sample data. They are not a strategy, have not been back-tested, and no
claim is made about their profitability or reliability. Replace them with rules
that are specified and validated through proper research.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass

from ..regime import MarketRegime
from .interfaces import BreakoutCandidate, RuleSet
from .models import CandleContext, Direction, Setup


@dataclass(frozen=True)
class CprCrossBreakout:
    """Candle opens at/below ``cpr_top`` and closes above it by ``buffer_pct`` (breakout),
    or opens at/above ``cpr_bottom`` and closes below it (breakdown)."""

    buffer_pct: float = 0.02
    allowed_regimes: Collection[MarketRegime] | None = None

    def detect(self, ctx: CandleContext) -> BreakoutCandidate | None:
        if self.allowed_regimes is not None and ctx.regime not in self.allowed_regimes:
            return None
        top, bottom = ctx.feature("cpr_top"), ctx.feature("cpr_bottom")
        buf = self.buffer_pct / 100.0
        if not math.isnan(top) and ctx.open <= top and ctx.close > top * (1 + buf):
            return BreakoutCandidate(Direction.BREAKOUT, top, "cpr_top", "close above previous-session CPR top")
        if not math.isnan(bottom) and ctx.open >= bottom and ctx.close < bottom * (1 - buf):
            return BreakoutCandidate(Direction.BREAKDOWN, bottom, "cpr_bottom",
                                     "close below previous-session CPR bottom")
        return None


@dataclass(frozen=True)
class LevelRetest:
    """Price trades back within ``tolerance_pct`` of the level while closing on the breakout side."""

    tolerance_pct: float = 0.03

    def is_retest(self, ctx: CandleContext, setup: Setup) -> bool:
        tol = self.tolerance_pct / 100.0
        if setup.direction is Direction.BREAKOUT:
            return ctx.low <= setup.level * (1 + tol) and ctx.close > setup.level
        return ctx.high >= setup.level * (1 - tol) and ctx.close < setup.level


@dataclass(frozen=True)
class RetestExtremeConfirmation:
    """Close beyond the retest candle's high (breakout) / low (breakdown), optionally
    requiring ``rvol >= min_rvol`` (NaN RVOL never confirms when a minimum is set)."""

    min_rvol: float | None = None

    def is_confirmed(self, ctx: CandleContext, setup: Setup) -> bool:
        assert setup.retest is not None
        if self.min_rvol is not None:
            rvol = ctx.feature("rvol")
            if math.isnan(rvol) or rvol < self.min_rvol:
                return False
        if setup.direction is Direction.BREAKOUT:
            return ctx.close > setup.retest.high
        return ctx.close < setup.retest.low


@dataclass(frozen=True)
class CloseBeyondLevelInvalidation:
    """Invalidate when a candle closes back through the level by ``buffer_pct``."""

    buffer_pct: float = 0.05

    def invalidation_reason(self, ctx: CandleContext, setup: Setup) -> str | None:
        buf = self.buffer_pct / 100.0
        if setup.direction is Direction.BREAKOUT and ctx.close < setup.level * (1 - buf):
            return f"close back below {setup.level_name}"
        if setup.direction is Direction.BREAKDOWN and ctx.close > setup.level * (1 + buf):
            return f"close back above {setup.level_name}"
        return None


def example_ruleset(
    *,
    breakout_buffer_pct: float = 0.02,
    retest_tolerance_pct: float = 0.03,
    invalidation_buffer_pct: float = 0.05,
    confirmation_min_rvol: float | None = None,
) -> RuleSet:
    """Assemble the UNVALIDATED example rules."""
    return RuleSet(
        breakout=CprCrossBreakout(buffer_pct=breakout_buffer_pct),
        retest=LevelRetest(tolerance_pct=retest_tolerance_pct),
        confirmation=RetestExtremeConfirmation(min_rvol=confirmation_min_rvol),
        invalidation=CloseBeyondLevelInvalidation(buffer_pct=invalidation_buffer_pct),
    )
