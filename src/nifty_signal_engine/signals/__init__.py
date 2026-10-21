"""Alert-only setup state machine. No order placement exists anywhere in this package."""

from .interfaces import (
    BreakoutCandidate,
    BreakoutDetector,
    ConfirmationRule,
    InvalidationRule,
    RetestDetector,
    RuleSet,
)
from .models import Alert, CandleContext, Direction, Setup, SetupState, StateTransition
from .runner import SignalRunResult, run_signals
from .state_machine import OutOfOrderCandleError, SignalStateMachine, StateMachineConfig, StepResult

__all__ = [
    "Alert",
    "BreakoutCandidate",
    "BreakoutDetector",
    "CandleContext",
    "ConfirmationRule",
    "Direction",
    "InvalidationRule",
    "OutOfOrderCandleError",
    "RetestDetector",
    "RuleSet",
    "Setup",
    "SetupState",
    "SignalRunResult",
    "SignalStateMachine",
    "StateMachineConfig",
    "StateTransition",
    "StepResult",
    "run_signals",
]
