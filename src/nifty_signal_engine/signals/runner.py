"""Feed a causal feature frame through a state machine, one candle at a time."""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import pandas as pd

from ..regime import MarketRegime
from ..schema import CLOSE_COL, HIGH_COL, LOW_COL, OPEN_COL, SCHEMA_COLUMNS, TIMESTAMP_COL, VOLUME_COL
from .models import Alert, CandleContext, StateTransition
from .state_machine import SignalStateMachine


@dataclass
class SignalRunResult:
    alerts: list[Alert] = field(default_factory=list)
    transitions: list[StateTransition] = field(default_factory=list)

    def alerts_frame(self) -> pd.DataFrame:
        return pd.DataFrame([a.__dict__ for a in self.alerts])

    def transitions_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.transitions])


def iter_contexts(frame: pd.DataFrame, feature_columns: Sequence[str] | None = None) -> Iterator[CandleContext]:
    """Yield a :class:`CandleContext` per row. ``frame`` must hold only causal features."""
    if feature_columns is None:
        feature_columns = [c for c in frame.columns if c not in (*SCHEMA_COLUMNS, "regime")]
    records = frame[list(feature_columns)].to_dict("records")
    has_volume = VOLUME_COL in frame.columns
    has_regime = "regime" in frame.columns
    for i, (row, feats) in enumerate(zip(frame.itertuples(index=False), records, strict=True)):
        volume = getattr(row, VOLUME_COL) if has_volume else None
        yield CandleContext(
            index=i,
            timestamp=getattr(row, TIMESTAMP_COL),
            open=float(getattr(row, OPEN_COL)),
            high=float(getattr(row, HIGH_COL)),
            low=float(getattr(row, LOW_COL)),
            close=float(getattr(row, CLOSE_COL)),
            volume=None if volume is None or math.isnan(volume) else float(volume),
            regime=MarketRegime(row.regime) if has_regime else MarketRegime.UNKNOWN,
            features=MappingProxyType(feats),
        )


def run_signals(
    frame: pd.DataFrame,
    machine: SignalStateMachine,
    feature_columns: Sequence[str] | None = None,
) -> SignalRunResult:
    result = SignalRunResult()
    for ctx in iter_contexts(frame, feature_columns):
        step = machine.step(ctx)
        result.transitions.extend(step.transitions)
        result.alerts.extend(step.alerts)
    return result
