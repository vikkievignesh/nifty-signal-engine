"""Settings loaded from environment variables (prefix ``NIFTY_``).

No credentials are read or stored here. Future broker/API credentials must come
from the environment or a secret manager, never from source files.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .features.volume import RelativeVolumeMode
from .regime import RegimeConfig
from .schema import EXPECTED_CANDLES_PER_SESSION, TRADED_VOLUME_SOURCES, VolumeSource
from .signals.state_machine import StateMachineConfig

PREFIX = "NIFTY_"
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


@dataclass(frozen=True)
class RuleParams:
    """Parameters for the UNVALIDATED example rules (see signals.example_rules)."""

    breakout_buffer_pct: float = 0.02
    retest_tolerance_pct: float = 0.03
    invalidation_buffer_pct: float = 0.05
    confirmation_min_rvol: float | None = None

    def __post_init__(self) -> None:
        for name in ("breakout_buffer_pct", "retest_tolerance_pct", "invalidation_buffer_pct"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} must be a finite, non-negative number, got {value!r}")
        if self.confirmation_min_rvol is not None and (
            not math.isfinite(self.confirmation_min_rvol) or self.confirmation_min_rvol <= 0
        ):
            raise ConfigError("confirmation_min_rvol must be a finite, positive number")


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    volume_source: VolumeSource = VolumeSource.NONE
    rvol_mode: RelativeVolumeMode = RelativeVolumeMode.ROLLING
    rvol_lookback: int = 20
    ema_fast: int = 9
    ema_slow: int = 21
    sma_period: int = 20
    cpr_min_session_candles: int = EXPECTED_CANDLES_PER_SESSION
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    state_machine: StateMachineConfig = field(default_factory=StateMachineConfig)
    rules: RuleParams = field(default_factory=RuleParams)

    def __post_init__(self) -> None:
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(f"log level must be one of {sorted(_LOG_LEVELS)}")
        for name in ("rvol_lookback", "ema_fast", "ema_slow", "sma_period", "cpr_min_session_candles"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")
        if self.ema_fast >= self.ema_slow:
            raise ConfigError("ema_fast must be shorter than ema_slow")
        if self.rules.confirmation_min_rvol is not None and self.volume_source not in TRADED_VOLUME_SOURCES:
            raise ConfigError(
                "RULE_CONFIRMATION_MIN_RVOL requires a traded-volume VOLUME_SOURCE "
                f"({sorted(s.value for s in TRADED_VOLUME_SOURCES)}); RVOL would be NaN and nothing could confirm"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        r = _Reader(env)
        try:
            return cls(
                log_level=r.str("LOG_LEVEL", "INFO").upper(),
                data_dir=Path(r.str("DATA_DIR", "data")),
                volume_source=r.enum("VOLUME_SOURCE", VolumeSource, VolumeSource.NONE),
                rvol_mode=r.enum("RVOL_MODE", RelativeVolumeMode, RelativeVolumeMode.ROLLING),
                rvol_lookback=r.int("RVOL_LOOKBACK", 20),
                ema_fast=r.int("EMA_FAST", 9),
                ema_slow=r.int("EMA_SLOW", 21),
                sma_period=r.int("SMA_PERIOD", 20),
                cpr_min_session_candles=r.int("CPR_MIN_SESSION_CANDLES", EXPECTED_CANDLES_PER_SESSION),
                regime=RegimeConfig(
                    slope_lookback=r.int("REGIME_SLOPE_LOOKBACK", 6),
                    trend_slope_min_pct=r.float("REGIME_TREND_SLOPE_MIN_PCT", 0.02),
                    range_spread_max_pct=r.float("REGIME_RANGE_SPREAD_MAX_PCT", 0.05),
                    use_cpr_filter=r.bool("REGIME_USE_CPR_FILTER", True),
                ),
                state_machine=StateMachineConfig(
                    max_bars_to_retest=r.int("SM_MAX_BARS_TO_RETEST", 6),
                    max_bars_to_confirm=r.int("SM_MAX_BARS_TO_CONFIRM", 3),
                    alert_ttl_bars=r.int("SM_ALERT_TTL_BARS", 3),
                    expire_at_session_end=r.bool("SM_EXPIRE_AT_SESSION_END", True),
                ),
                rules=RuleParams(
                    breakout_buffer_pct=r.float("RULE_BREAKOUT_BUFFER_PCT", 0.02),
                    retest_tolerance_pct=r.float("RULE_RETEST_TOLERANCE_PCT", 0.03),
                    invalidation_buffer_pct=r.float("RULE_INVALIDATION_BUFFER_PCT", 0.05),
                    confirmation_min_rvol=r.optional_float("RULE_CONFIRMATION_MIN_RVOL"),
                ),
            )
        except ConfigError:
            raise
        except ValueError as exc:  # invalid values rejected by nested configs
            raise ConfigError(str(exc)) from exc


class _Reader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def _raw(self, key: str) -> str | None:
        value = self.env.get(PREFIX + key)
        return None if value is None or value.strip() == "" else value.strip()

    def str(self, key: str, default: str) -> str:
        return self._raw(key) or default

    def int(self, key: str, default: int) -> int:
        raw = self._raw(key)
        try:
            return default if raw is None else int(raw)
        except ValueError:
            raise ConfigError(f"{PREFIX}{key} must be an integer, got {raw!r}") from None

    def float(self, key: str, default: float) -> float:
        value = self.optional_float(key)
        return default if value is None else value

    def optional_float(self, key: str) -> float | None:
        raw = self._raw(key)
        try:
            value = None if raw is None else float(raw)
        except ValueError:
            raise ConfigError(f"{PREFIX}{key} must be a number, got {raw!r}") from None
        if value is not None and not math.isfinite(value):
            raise ConfigError(f"{PREFIX}{key} must be finite, got {raw!r}")
        return value

    def bool(self, key: str, default: bool) -> bool:
        raw = self._raw(key)
        if raw is None:
            return default
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise ConfigError(f"{PREFIX}{key} must be a boolean, got {raw!r}")

    def enum(self, key, enum_cls, default):
        raw = self._raw(key)
        try:
            return default if raw is None else enum_cls(raw.lower())
        except ValueError:
            raise ConfigError(
                f"{PREFIX}{key} must be one of {[e.value for e in enum_cls]}, got {raw!r}"
            ) from None
