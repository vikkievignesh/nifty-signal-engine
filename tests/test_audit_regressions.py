"""Regression tests for defects found in the repository audit."""

import re
from pathlib import Path
from types import MappingProxyType

import pandas as pd
import pytest

from nifty_signal_engine.config import ConfigError, RuleParams, Settings
from nifty_signal_engine.data_loader import load_candles_csv
from nifty_signal_engine.features.cpr import compute_cpr
from nifty_signal_engine.pipeline import FEATURE_COLUMNS, build_example_machine, build_feature_frame
from nifty_signal_engine.regime import MarketRegime, RegimeConfig
from nifty_signal_engine.schema import IST, SCHEMA_COLUMNS, VolumeSource, candle_close_time
from nifty_signal_engine.signals import (
    CandleContext,
    Direction,
    Setup,
    SignalStateMachine,
    StateMachineConfig,
    run_signals,
)
from nifty_signal_engine.signals.example_rules import example_ruleset
from nifty_signal_engine.signals.runner import iter_contexts
from nifty_signal_engine.synthetic import generate_synthetic_candles
from nifty_signal_engine.validation import CandleValidationError, validate_candles

from conftest import make_candles

REPO = Path(__file__).resolve().parents[1]
GOOD_ROW = "2024-01-02 09:15,100,101,99,100"


def write_csv(tmp_path, text, name="c.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- loader: every bad input is a CandleValidationError, not a pandas/Python error -------------

@pytest.mark.parametrize("text, match", [
    ("", "empty"),
    ("timestamp,open,high,low,close\n", "no candles"),
    (f"timestamp,open,high,low,close\n{GOOD_ROW}".replace("2024-01-02 09:15", "not-a-time"), "unparseable"),
    (f"timestamp,open,high,low,close\n{GOOD_ROW}".replace("2024-01-02 09:15", "1704167100"), "unparseable"),
    ("timestamp,Close,close,open,high,low\n2024-01-02 09:15,1,2,100,101,99\n", "duplicate column"),
])
def test_loader_bad_input_raises_candle_validation_error(tmp_path, text, match):
    with pytest.raises(CandleValidationError, match=match):
        load_candles_csv(write_csv(tmp_path, text))


def test_loader_accepts_utf8_bom_header(tmp_path):  # Excel exports
    df = load_candles_csv(write_csv(tmp_path, "﻿timestamp,open,high,low,close\n" + GOOD_ROW + "\n"))
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close"]


# --- CPR must never be derived from unordered candles -----------------------------------------

def test_cpr_rejects_shuffled_candles(synthetic):
    with pytest.raises(ValueError, match="ascending"):
        compute_cpr(synthetic.sample(frac=1, random_state=0))


def test_cpr_rejects_swap_inside_a_session():
    # Used to pass silently and change the next session's pivot (wrong "last" close).
    candles = generate_synthetic_candles(2, seed=1)
    swapped = candles.copy()
    swapped.iloc[[73, 74]] = swapped.iloc[[74, 73]].to_numpy()
    with pytest.raises(ValueError, match="ascending"):
        compute_cpr(swapped)


def test_cpr_rejects_duplicate_timestamps(synthetic):
    with pytest.raises(ValueError, match="ascending"):
        compute_cpr(pd.concat([synthetic, synthetic.iloc[[5]]], ignore_index=True).sort_values("timestamp"))


# --- alerts must not stay active after the session closes -------------------------------------

def _alert_at(hour, minute, **cfg):
    ts = pd.Timestamp(f"2024-01-02 {hour:02d}:{minute:02d}", tz=IST)
    ctx = CandleContext(9, ts, 1, 1, 1, 1, None, MarketRegime.MIXED, MappingProxyType({}))
    setup = Setup("s", Direction.BREAKOUT, 1.0, "lvl", ts.normalize(), ts, 0)
    machine = SignalStateMachine(example_ruleset(), StateMachineConfig(**cfg))
    return machine._make_alert(setup, ctx, "r")


def test_alert_expiry_unchanged_mid_session():
    a = _alert_at(10, 0, alert_ttl_bars=3)
    assert a.expires_at - a.available_at == pd.Timedelta(minutes=15)


def test_alert_expiry_capped_at_session_close():
    a = _alert_at(15, 15, alert_ttl_bars=3)  # available 15:20, ttl would reach 15:35
    assert a.expires_at == pd.Timestamp("2024-01-02 15:30", tz=IST)
    assert a.is_active(pd.Timestamp("2024-01-02 15:25", tz=IST))
    assert not a.is_active(pd.Timestamp("2024-01-02 15:30", tz=IST))


def test_alert_on_final_candle_is_never_active():
    a = _alert_at(15, 25)  # available exactly at the close
    assert a.expires_at == a.available_at
    assert not a.is_active(a.available_at)


def test_alert_expiry_cap_can_be_disabled():
    a = _alert_at(15, 15, alert_ttl_bars=3, expire_at_session_end=False)
    assert a.expires_at == pd.Timestamp("2024-01-02 15:35", tz=IST)


# --- configuration -----------------------------------------------------------------------------

@pytest.mark.parametrize("env", [
    {"NIFTY_RULE_BREAKOUT_BUFFER_PCT": "-5"},
    {"NIFTY_RULE_RETEST_TOLERANCE_PCT": "nan"},
    {"NIFTY_RULE_INVALIDATION_BUFFER_PCT": "inf"},
    {"NIFTY_REGIME_TREND_SLOPE_MIN_PCT": "nan"},
    {"NIFTY_REGIME_RANGE_SPREAD_MAX_PCT": "-0.1"},
    {"NIFTY_RULE_CONFIRMATION_MIN_RVOL": "0", "NIFTY_VOLUME_SOURCE": "index_futures"},
    {"NIFTY_RULE_CONFIRMATION_MIN_RVOL": "-1", "NIFTY_VOLUME_SOURCE": "index_futures"},
])
def test_config_rejects_nonfinite_or_negative_thresholds(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


@pytest.mark.parametrize("source", ["none", "index_reported"])
def test_config_rejects_rvol_requirement_without_traded_volume(source):
    # Otherwise RVOL is NaN everywhere and no setup could ever confirm — silently.
    with pytest.raises(ConfigError, match="traded-volume"):
        Settings.from_env({"NIFTY_RULE_CONFIRMATION_MIN_RVOL": "1.5", "NIFTY_VOLUME_SOURCE": source})


def test_config_rvol_requirement_ok_with_traded_volume():
    s = Settings.from_env({"NIFTY_RULE_CONFIRMATION_MIN_RVOL": "1.5", "NIFTY_VOLUME_SOURCE": "etf_proxy"})
    assert s.rules.confirmation_min_rvol == 1.5


def test_rule_params_validate_directly():
    with pytest.raises(ConfigError):
        RuleParams(breakout_buffer_pct=-1)


def test_regime_config_rejects_nan():
    with pytest.raises(ValueError):
        RegimeConfig(trend_slope_min_pct=float("nan"))


def test_env_example_documents_every_variable_the_code_reads():
    code = (REPO / "src/nifty_signal_engine/config.py").read_text()
    read = set(re.findall(r'\br\.\w+\(\s*"([A-Z_]+)"', code))
    documented = set(re.findall(r"^#?\s*NIFTY_([A-Z_]+)=", (REPO / ".env.example").read_text(), re.M))
    assert read, "pattern found no variables; test is stale"
    assert read == documented


# --- pipeline guards ---------------------------------------------------------------------------

def test_pipeline_rejects_unordered_or_duplicated_candles(synthetic):
    with pytest.raises(CandleValidationError):
        build_feature_frame(synthetic.sample(frac=1, random_state=1), Settings())
    with pytest.raises(CandleValidationError):
        build_feature_frame(pd.concat([synthetic, synthetic.iloc[[3]]], ignore_index=True), Settings())


def test_pipeline_is_independent_of_the_input_index(synthetic):
    window = synthetic.iloc[40:200]
    a = build_feature_frame(window, Settings(volume_source=VolumeSource.INDEX_FUTURES)).reset_index(drop=True)
    b = build_feature_frame(window.reset_index(drop=True), Settings(volume_source=VolumeSource.INDEX_FUTURES))
    pd.testing.assert_frame_equal(a, b)


def test_feature_allow_list_is_complete_and_excludes_candle_columns(synthetic):
    frame = build_feature_frame(synthetic, Settings())
    assert set(FEATURE_COLUMNS) <= set(frame.columns)
    assert not set(FEATURE_COLUMNS) & set(SCHEMA_COLUMNS)


def test_extra_research_columns_never_reach_rules(synthetic):
    frame = build_feature_frame(synthetic, Settings())
    frame["future_return"] = frame["close"].shift(-12) / frame["close"] - 1  # deliberately forward-looking
    ctx = next(iter_contexts(frame, FEATURE_COLUMNS))
    assert set(ctx.features) == set(FEATURE_COLUMNS)
    assert "future_return" not in ctx.features


def test_run_signals_identical_with_or_without_extra_columns(synthetic):
    settings = Settings(volume_source=VolumeSource.INDEX_FUTURES)
    frame = build_feature_frame(generate_synthetic_candles(8, seed=3), settings)
    base = run_signals(frame, build_example_machine(settings), FEATURE_COLUMNS)
    polluted = frame.assign(future_return=frame["close"].shift(-12))
    other = run_signals(polluted, build_example_machine(settings), FEATURE_COLUMNS)
    assert base.transitions == other.transitions and base.alerts == other.alerts


def test_runner_result_frames(synthetic):
    settings = Settings(volume_source=VolumeSource.INDEX_FUTURES)
    frame = build_feature_frame(generate_synthetic_candles(10, seed=7), settings)
    result = run_signals(frame, build_example_machine(settings), FEATURE_COLUMNS)
    assert len(result.transitions_frame()) == len(result.transitions) > 0
    assert len(result.alerts_frame()) == len(result.alerts)
    assert all(a.validated is False for a in result.alerts)


# --- validation / schema gaps ------------------------------------------------------------------

def test_validate_empty_frame():
    empty = make_candles("2024-01-02", [100]).iloc[0:0]
    assert validate_candles(empty).errors == ["no candles"]


def test_validate_missing_timestamp_value():
    df = make_candles("2024-01-02", [100, 101, 102])
    df.loc[1, "timestamp"] = pd.NaT
    assert any("missing timestamps" in e for e in validate_candles(df).errors)


def test_validate_non_numeric_columns():
    df = make_candles("2024-01-02", [100, 101], volume=[1, 2])
    df["close"] = df["close"].astype(str)
    df["volume"] = df["volume"].astype(str)
    errors = " ".join(validate_candles(df).errors)
    assert "'close' is not numeric" in errors


def test_validate_non_numeric_volume_only():
    df = make_candles("2024-01-02", [100, 101], volume=[1, 2])
    df["volume"] = ["a", "b"]
    assert any("'volume' is not numeric" in e for e in validate_candles(df).errors)


def test_validate_weekend_warns():
    df = make_candles("2024-01-06", [100, 101])  # Saturday (special sessions do occur)
    report = validate_candles(df)
    assert report.ok and any("weekend" in w for w in report.warnings)


def test_candle_close_time_is_open_plus_interval():
    assert candle_close_time(pd.Timestamp("2024-01-02 15:25", tz=IST)) == pd.Timestamp("2024-01-02 15:30", tz=IST)
