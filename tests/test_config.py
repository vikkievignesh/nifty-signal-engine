from pathlib import Path

import pytest

from nifty_signal_engine.config import ConfigError, Settings
from nifty_signal_engine.features import RelativeVolumeMode
from nifty_signal_engine.schema import VolumeSource


def test_defaults_from_empty_env():
    s = Settings.from_env({})
    assert s == Settings()
    assert s.volume_source is VolumeSource.NONE
    assert s.rules.confirmation_min_rvol is None


def test_overrides():
    s = Settings.from_env({
        "NIFTY_LOG_LEVEL": "debug",
        "NIFTY_DATA_DIR": "/tmp/x",
        "NIFTY_VOLUME_SOURCE": "INDEX_FUTURES",
        "NIFTY_RVOL_MODE": "time_of_day",
        "NIFTY_EMA_FAST": "5",
        "NIFTY_EMA_SLOW": "13",
        "NIFTY_REGIME_USE_CPR_FILTER": "false",
        "NIFTY_SM_ALERT_TTL_BARS": "2",
        "NIFTY_RULE_CONFIRMATION_MIN_RVOL": "1.5",
    })
    assert s.log_level == "DEBUG" and s.data_dir == Path("/tmp/x")
    assert s.volume_source is VolumeSource.INDEX_FUTURES
    assert s.rvol_mode is RelativeVolumeMode.TIME_OF_DAY
    assert (s.ema_fast, s.ema_slow) == (5, 13)
    assert s.regime.use_cpr_filter is False
    assert s.state_machine.alert_ttl_bars == 2
    assert s.rules.confirmation_min_rvol == 1.5


@pytest.mark.parametrize("env", [
    {"NIFTY_EMA_FAST": "abc"},
    {"NIFTY_EMA_FAST": "30", "NIFTY_EMA_SLOW": "21"},
    {"NIFTY_VOLUME_SOURCE": "nse_magic"},
    {"NIFTY_REGIME_USE_CPR_FILTER": "maybe"},
    {"NIFTY_LOG_LEVEL": "LOUD"},
    {"NIFTY_SM_MAX_BARS_TO_RETEST": "0"},
    {"NIFTY_RVOL_LOOKBACK": "-3"},
])
def test_invalid_values(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)
