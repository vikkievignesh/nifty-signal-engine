"""Data types shared by the signal state machine."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from ..regime import MarketRegime
from ..schema import CANDLE_INTERVAL


class Direction(StrEnum):
    BREAKOUT = "BREAKOUT"  # price moved above a reference level
    BREAKDOWN = "BREAKDOWN"  # price moved below a reference level


class SetupState(StrEnum):
    IDLE = "IDLE"
    BREAKOUT_DETECTED = "BREAKOUT_DETECTED"  # level broken (either direction)
    RETEST = "RETEST"  # pullback/retest of the broken level observed
    CONFIRMED = "CONFIRMED"  # terminal: alert emitted
    INVALIDATED = "INVALIDATED"  # terminal
    EXPIRED = "EXPIRED"  # terminal


TERMINAL_STATES = frozenset({SetupState.CONFIRMED, SetupState.INVALIDATED, SetupState.EXPIRED})


@dataclass(frozen=True)
class CandleContext:
    """Everything the rules may look at for one *completed* candle.

    ``features`` must only contain values computable at the candle's close.
    """

    index: int
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    regime: MarketRegime
    features: Mapping[str, float]

    @property
    def available_at(self) -> pd.Timestamp:
        return self.timestamp + CANDLE_INTERVAL

    @property
    def session_date(self) -> pd.Timestamp:
        return self.timestamp.normalize()

    def feature(self, name: str) -> float:
        """Feature value or NaN if absent."""
        value = self.features.get(name, math.nan)
        return math.nan if value is None else float(value)


@dataclass
class Setup:
    """A tracked breakout/breakdown setup (mutable, owned by the state machine)."""

    setup_id: str
    direction: Direction
    level: float
    level_name: str
    session_date: pd.Timestamp
    detected_at: pd.Timestamp
    detected_index: int
    state: SetupState = SetupState.BREAKOUT_DETECTED
    retest: CandleContext | None = None


@dataclass(frozen=True)
class StateTransition:
    timestamp: pd.Timestamp
    setup_id: str
    from_state: SetupState
    to_state: SetupState
    reason: str


@dataclass(frozen=True)
class Alert:
    """A research alert. ``validated`` is always False: no rule here has been validated."""

    alert_id: str
    setup_id: str
    direction: Direction
    level: float
    level_name: str
    candle_timestamp: pd.Timestamp
    available_at: pd.Timestamp
    expires_at: pd.Timestamp
    regime: MarketRegime
    reason: str
    validated: bool = False

    def is_active(self, now: pd.Timestamp) -> bool:
        return self.available_at <= now < self.expires_at
