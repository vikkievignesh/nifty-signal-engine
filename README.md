# NIFTY Signal Engine

A research-first, **alert-only** engine for NIFTY 50 intraday signals on
five-minute candles.

> **Status: research scaffold.** No signal rule in this repository has been
> validated. No profitability or reliability is claimed. The engine does not
> place orders, connect to a broker, or call any live API.

## Project layout

```
src/nifty_signal_engine/
  schema.py           Candle schema, IST timezone, session constants, VolumeSource
  validation.py       validate_candles() -> ValidationReport
  data_loader.py      load_candles_csv(): parse, normalise to IST, validate
  features/
    cpr.py            Central Pivot Range from the previous completed session
    moving_averages.py EMA / SMA
    volume.py         Relative volume (baseline excludes the current candle)
  regime.py           Preliminary regime classifier (UNVALIDATED thresholds)
  signals/
    models.py         CandleContext, Setup, Alert, StateTransition, enums
    interfaces.py     Rule protocols: breakout, retest, confirmation, invalidation
    state_machine.py  Breakout -> retest -> confirmation state machine, expiry
    example_rules.py  UNVALIDATED placeholder rules used to exercise the machine
    runner.py         Feed a feature frame through the machine candle by candle
  pipeline.py         build_feature_frame(): CPR + EMA/SMA + RVOL + regime
  config.py           Settings.from_env() (NIFTY_* environment variables)
  logging_config.py   Stdlib logging setup
  synthetic.py        Deterministic synthetic candles for tests/demos
  __main__.py         python -m nifty_signal_engine --csv <file>
tests/                pytest suite
data/sample/          Synthetic sample CSV (not market data)
scripts/              generate_sample_data.py
```

## Setup and tests

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Run the demo pipeline on the synthetic sample:

```bash
NIFTY_VOLUME_SOURCE=index_futures python -m nifty_signal_engine
# or: python -m nifty_signal_engine --csv path/to/your.csv
```

## Candle schema

| column      | type                                  | meaning                       |
|-------------|---------------------------------------|-------------------------------|
| `timestamp` | tz-aware datetime, `Asia/Kolkata`     | candle **open** time          |
| `open`      | float64                               | first price in the interval   |
| `high`      | float64                               | highest price                 |
| `low`       | float64                               | lowest price                  |
| `close`     | float64                               | last price                    |
| `volume`    | float64, optional, may be NaN         | see *Volume sources*          |

* A candle labelled `09:15` covers `[09:15, 09:20)` and is usable only at
  `09:20` (`schema.candle_close_time`). Alerts carry both `candle_timestamp`
  and `available_at`.
* Regular session: 09:15–15:30 IST → 75 candles labelled 09:15 … 15:25.
* CSV timestamps must be ISO-8601. Values with a UTC offset are converted to
  IST; values without an offset are interpreted as IST; a mix is rejected.
* Validation errors: missing columns, non-IST/naive timestamps, duplicates,
  unsorted rows, off-grid times, candles outside session hours (can be
  relaxed for special sessions), non-positive/missing prices, inconsistent
  OHLC, negative volume. Warnings: weekend candles, missing volume, sessions
  without 75 candles. The loader never silently sorts or de-duplicates.

## Volume sources

NIFTY 50 is an index; it does not trade, so "volume" shown on the spot index by
vendors is **not** traded volume and its meaning varies by vendor. Set
`NIFTY_VOLUME_SOURCE` to describe what your `volume` column is:

| value                   | RVOL supported | notes                                            |
|-------------------------|----------------|--------------------------------------------------|
| `none`                  | no             | default                                          |
| `index_reported`        | no             | vendor spot-index volume; not a traded measure   |
| `index_futures`         | yes            | NIFTY futures volume; handle contract rollovers  |
| `constituent_aggregate` | yes            | summed constituent stock volume                  |
| `etf_proxy`             | yes            | NIFTY ETF volume; liquidity is a caveat          |

For unsupported sources RVOL columns are NaN in the pipeline;
`relative_volume()` raises unless `allow_unsupported_source=True`.

RVOL modes: `rolling` (previous N candles, excluding the current one) and
`time_of_day` (same intraday slot over the previous N sessions, excluding the
current session).

## Look-ahead safeguards

* CPR for session D uses only the latest session strictly before D; an
  incomplete previous session (fewer than `NIFTY_CPR_MIN_SESSION_CANDLES`
  candles) yields NaN rather than partial-day levels.
* EMA/SMA/regime values at row t use rows ≤ t; RVOL baselines exclude row t.
* The state machine processes one completed candle at a time and rejects
  out-of-order candles; breakout, retest and confirmation occur on distinct
  candles.
* Tests assert prefix invariance: features and alerts computed on candles
  `[0, t]` equal those computed on the full data truncated at t, and shocking
  future candles leaves past features unchanged.

## Regime classifier (UNVALIDATED)

`TREND_UP`, `TREND_DOWN`, `RANGE`, `MIXED`, `UNKNOWN` from close vs fast/slow
EMA, slow-EMA slope and (optionally) position vs previous-session CPR. See
`regime.py` for the exact rules. The thresholds are placeholders, not results.

## Signal state machine

```
IDLE → BREAKOUT_DETECTED → RETEST → CONFIRMED (alert)
            ↘ INVALIDATED / EXPIRED (timeout or session end)
```

Rules plug in through the protocols in `signals/interfaces.py`. The rules in
`signals/example_rules.py` (CPR cross, level retest, retest-extreme
confirmation, close-back invalidation) are **placeholders** so the scaffold runs
end to end. Alerts have `validated=False` and an expiry
(`NIFTY_SM_ALERT_TTL_BARS`).

## Configuration

All settings are environment variables with the `NIFTY_` prefix; see
`.env.example` for the full list and defaults. The engine does not auto-load
`.env` files. Invalid values raise `ConfigError` at start-up.

## Credentials

No credentials are used yet. Future API keys (e.g. FYERS) must be supplied via
environment variables or a secret manager and never committed. `.env`, key
files and token files are git-ignored.

## Assumptions and limitations

* No NSE holiday calendar: "previous session" means the previous session
  present in the data. Gaps > 4 calendar days are logged.
* Daily H/L/C for CPR are derived from the 5-minute candles by default; NSE's
  official close can differ. Pass official values via `compute_cpr(daily=...)`.
* Moving averages run continuously across sessions (no daily reset).
* Sample data is synthetic and says nothing about real market behaviour.
