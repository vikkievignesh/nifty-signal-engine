# NIFTY Signal Engine

A research-first, **alert-only** engine for NIFTY 50 intraday signals on
five-minute candles.

> **Status: research scaffold.** No signal rule in this repository has been
> validated. No profitability or reliability is claimed. The engine does not
> place orders, stream data or send notifications. The only code that touches the
> network is the optional, local FYERS tooling in `nifty_signal_engine.fyers` -- the read-only
> history downloader and the interactive login helper (a loopback callback server plus one
> token-exchange request) -- which runs only when you invoke it.

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
  fyers/              Local-only FYERS tools: history downloader, plus login helper (auth, envfile, login)
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

## Continuous integration

`.github/workflows/ci.yml` runs the full `pytest` suite on every push and pull
request across Python 3.11, 3.12 and 3.13, each with the lowest and the latest
pandas/numpy that the project's `pyproject.toml` allows (six jobs). It installs
with `pip install -e ".[dev]"`, uses no secrets, and makes no network calls
beyond installing packages from PyPI.

| Python    | `lowest` pins              | `latest`                          |
|-----------|----------------------------|-----------------------------------|
| 3.11/3.12 | pandas 2.2.0, numpy 1.26.0 | whatever pip resolves             |
| 3.13      | pandas 2.2.3, numpy 2.1.0  | whatever pip resolves             |

The 3.13 floor is higher because numpy 1.26 and pandas 2.2.0–2.2.2 have no
Python 3.13 support. `tests/test_ci_config.py` fails if the workflow drifts from
`pyproject.toml` (Python versions, pandas/numpy pins, secrets, privileged
triggers). pandas 2.2.0 prints a harmless pyarrow notice on import.

## Downloading history from FYERS (local, read-only)

`python -m nifty_signal_engine.fyers` downloads completed 5-minute candles for
`NSE:NIFTY50-INDEX` over REST into local CSV files that the existing loader and
validator accept. It does **not** stream, place orders, send notifications or deploy
anything. It has not been run against the real FYERS service in this repository's
development so far; the tests use mocked responses only.

**One-time manual setup (you do this, in your own terminal):**

1. In the FYERS API dashboard, create an app and note its *App ID*. Enable the
   market-data permission (historical data) for it.
2. Save `http://127.0.0.1:5000/callback` as the app's redirect URL, exactly as written
   (use another port only as described under *If port 5000 is busy* below), and note the
   app's *App Secret*.
3. Put your credentials in a local, git-ignored `.env` file in the repository root
   (or export them in your shell), then `chmod 600 .env`:

   ```
   FYERS_APP_ID=<your app id>
   FYERS_APP_SECRET=<your app secret>   # used only by the login helper
   ```

   Then run the login helper below, which writes `FYERS_ACCESS_TOKEN` into the same
   file. Real environment variables take precedence over `.env`. Prefer exporting
   `FYERS_APP_SECRET` for the one login command over keeping it in a file. The
   downloader itself never reads the secret. Never paste these values into chat,
   issues or commits.

**Logging in (local helper; replaces generating the token by hand):**

```bash
python -m nifty_signal_engine.fyers.login            # add --print-url if no browser can be opened
```

It follows the FYERS v3 Individual App flow: it opens the FYERS login page, listens
only on `http://127.0.0.1:<port>/callback` (loopback only; port 5000 unless you set one
explicitly; never a fallback), checks
the returned OAuth `state` against a fresh 256-bit random value, exchanges the
`auth_code` at `/validate-authcode` using `appIdHash = SHA256("app_id:app_secret")`,
and updates **only** `FYERS_ACCESS_TOKEN` in `.env` (all other lines are preserved;
file mode forced to `0600`; symlinks, non-regular files, foreign-owned or hard-linked
files and files git does not ignore are refused; the write is atomic). You complete the
FYERS login and 2FA in the browser yourself. The app secret, `appIdHash`, auth code and
access token are never printed or logged, and the login URL is hidden unless you pass
`--print-url`. Options: `--env-file`, `--port`, `--timeout` (seconds, max 600), `--print-url`,
`--no-browser`. Access tokens expire (daily, per FYERS); run it again when the
downloader reports an authentication failure. It places no orders and runs no trading
logic.

**If port 5000 is busy (common on macOS).** macOS often reserves port 5000 for its
AirPlay Receiver (it appears as *Control Center*), and the helper then refuses to start
with a "cannot listen on 127.0.0.1:5000" error. It never kills or changes another
process and never silently switches port. Either free the port yourself (for example by
turning AirPlay Receiver off in System Settings) or choose another port explicitly:

1. In the FYERS API dashboard, change the app's redirect URL to exactly
   `http://127.0.0.1:<port>/callback` (for port 5001: `http://127.0.0.1:5001/callback`)
   and **save it**. FYERS only accepts the redirect URL saved on the app, so this must be
   done before you log in.
2. Run the helper with the same port, either way:

   ```bash
   python -m nifty_signal_engine.fyers.login --port 5001
   # or: export FYERS_CALLBACK_PORT=5001   (the environment wins over .env; --port wins over both)
   ```

The port must be an integer from 1024 to 65535 (`0`, i.e. "any free port", is refused
because it could never match a saved URL). The `redirect_uri` sent to FYERS is always built
from the port the helper actually bound, and the helper prints the exact URL to save; a
custom port also prints a reminder. Nothing else changes: the host stays `127.0.0.1`, and
the OAuth `state` check and all other protections apply on every port. Going back to the
default needs the dashboard redirect URL set to `http://127.0.0.1:5000/callback` again.

**Troubleshooting `network error [category]` (login or download).** Network failures are
classified without ever printing raw exception text, so the message cannot contain an auth
code, secret, token, request body or credential-bearing URL:

| category | meaning | was the request sent? |
|---|---|---|
| `tls_certificate` | Python could not verify FYERS's certificate chain | no -- the auth code is unused |
| `tls` | TLS handshake failed for another reason | no |
| `dns` | host name did not resolve | no |
| `connection` | refused/unreachable (nothing sent) or dropped mid-request (maybe sent) | no / maybe |
| `timeout` | no answer in time | unknown -- the single-use auth code may be used up |
| `protocol` | malformed or truncated HTTP response | maybe |

HTTP-level problems are different: they arrive as an HTTP status (for example `HTTP 403`) in
the error, not as a `network error`. `curl` working while Python fails usually means the two
use different certificate stores: `curl` trusts the operating system's, while Python's
`urllib` uses its own CA bundle. On macOS builds from python.org that bundle is empty until you
run the *Install Certificates.command* that ships with Python, or `pip install certifi` and
`export SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())')`. A proxy or
antivirus that re-signs HTTPS traffic causes the same symptom. Never disable certificate
verification. To check each layer separately, with no credentials involved:

```bash
python -m nifty_signal_engine.fyers.diagnose
```

It tests DNS, TCP, TLS (with full certificate verification) and one plain HTTPS GET of the
host root of `api-t1.fyers.in` (not the token-exchange endpoint, with no headers or body), and
reports Python's trust setup as yes/no facts only (no paths or values). Certificate failures
are not retried; other transient failures are retried by the downloader but never by the
login, because an auth code can be used only once.

**Troubleshooting `token exchange rejected (HTTP nnn)`.** A rejection now says what kind of
response FYERS sent, without ever showing a raw body: the HTTP status, the response's `code` and
`message` if it is a JSON API error (only those allowlisted fields are read, and they are scrubbed of
your App ID, secret, hash and auth code in any case or URL-encoding, and of long opaque tokens), or
just the shape and size for anything else (for example *an HTML page, not a JSON API error*, as a
gateway or firewall block page would be). Bodies, other fields, headers and cookies are never shown.
That distinction matters: a JSON API error with a code points at the application's own validation,
while an HTML or code-less body points at something in front of it. The message does not name a
cause; none is documented for a 403 on this endpoint (as far as the available sources show).

Before another attempt, run the offline check (no server, no network, no secret printed):

```bash
python -m nifty_signal_engine.fyers.login --check --port 5001
```

It describes the exact request (`POST` to `/api/v3/validate-authcode`, JSON fields `grant_type`,
`appIdHash`, `code`), explains that `appIdHash` is the SHA-256 of `<App ID>:<App Secret>` using the **full
App ID exactly as the FYERS dashboard shows it, including its `-100`-style suffix** (dropping the suffix
changes the hash), flags an App ID that does not look like `<ID>-<digits>` or that is wrapped in quote
characters, and prints the redirect URI that will be sent, which must equal the saved dashboard value
character for character. Auth codes are single-use and short-lived: always start a fresh login.

**Experiment: explicit User-Agent for the token exchange (diagnostic only, off by default).**
By default the exchange request sets only `Content-Type: application/json`, and urllib adds its
own `User-Agent: Python-urllib/<python version>` (the official SDK, built on `requests`, sends a
different one). Whether FYERS reacts to that header is **unknown**: this option exists only to
find out, in one controlled attempt, and is not a fix. To send a different `User-Agent` for a
single login, and nothing else changed (same URL, same JSON body, same other headers, no
retries):

```bash
python -m nifty_signal_engine.fyers.login --port 5001 --user-agent "nifty-signal-engine-experiment/0.1"
python -m nifty_signal_engine.fyers.login --check --port 5001 --user-agent "nifty-signal-engine-experiment/0.1"   # offline preview
```

The value must be 1-200 printable ASCII characters (CR/LF and other control characters are refused
before anything is sent). The same opt-in flag exists on the historical-data downloader, with the same
validation, so a data request can be compared with and without it:

```bash
python -m nifty_signal_engine.fyers --start "$(date -v-7d +%F)" --user-agent "nifty-signal-engine-experiment/0.1"
```

On the downloader it is added to every history request (each window) and nothing else changes: the
same URL, the same `Authorization`/`Content-Type`/`version` headers, the same pacing and retries. The value
is never written to the CSV, metadata or raw files, and is not stored anywhere: dropping the flag
restores the default. The login prints a note whenever the
override is active. Compare the outcome of one attempt with and without the flag; a different
result is evidence, an identical one rules this header out.

*Security limitations:* anything running as your user can reach `127.0.0.1:<port>` while
the helper waits and could race the real redirect (a wrong `state` aborts the login,
and the page shown echoes nothing); the one-shot callback is plain HTTP on loopback
because that is what the redirect URL uses; a token in `.env` is as safe as your user
account and disk (consider full-disk encryption); the process holds the secret, code
and token in memory while it runs; `0600` and ownership checks are POSIX-only (on
Windows the file mode is not enforced); and checking that `.env` is git-ignored needs
`git` on the PATH (if git is unavailable the check is skipped).

*Not verified against the live FYERS service* (the official documentation pages could
not be opened from the development environment and no live login was performed): the
exact redirect query format and whether FYERS accepts a `http://127.0.0.1:<port>/callback`
redirect URL. The helper is deliberately strict: it accepts a callback only with a
matching `state`, exactly one well-formed `auth_code` and no `error`/`error_description`
parameter, and rejects anything else with a generic message. If your first attempt is
rejected, check the redirect URL registered on the app and report what FYERS actually
sends (without pasting any code, state or token).

**Run:**

```bash
python -m nifty_signal_engine.fyers --start 2024-01-01 --end 2024-03-31
python -m nifty_signal_engine --csv data/local/fyers/NSE_NIFTY50-INDEX_5m_2024-01-01_2024-03-31.csv
```

Options: `--out-dir`, `--window-days` (1-100, default 90), `--min-interval`
(seconds between requests, default 1), `--no-raw`, `--allow-off-hours`, and the experimental
`--user-agent` (see *Experiment: explicit User-Agent* below).

**Output** (default `data/local/fyers/`, git-ignored): the CSV
(`timestamp,open,high,low,close,volume`, ISO-8601 with `+05:30`), a `.meta.json`
(counts, gaps, duplicates, warnings) and `raw/*.json` raw responses per window. No
credentials or headers are written to any file; they are also scrubbed from logs
and error messages. If you set `NIFTY_DATA_DIR` to a path outside the ignored
folders, make sure that path is ignored too.

**Behaviour:** the range is split into windows of at most 100 calendar days (the
documented FYERS intraday limit). Epoch seconds are converted to Asia/Kolkata;
candles outside the requested dates and candles that have not closed yet are
dropped; duplicate timestamps are removed (the first is kept, conflicting
duplicates are counted); the result must pass `validate_candles`, otherwise
nothing is written (raw responses are kept for debugging) and the CSV is
re-loaded with `load_candles_csv` as a round-trip check. Requests are paced and
retried with backoff on HTTP 429/5xx and network errors; authentication failures
and other API errors are not retried. Weekdays without data (holidays or gaps) and
incomplete sessions are listed in the metadata; no holiday calendar is applied.

**Volume warning.** `NSE:NIFTY50-INDEX` is a spot index. Do **not** assume the
`volume` column FYERS returns for it is genuine traded volume. The metadata marks
it `index_reported`, and the engine's relative-volume feature refuses that source.
For volume research use futures, constituent-aggregate or ETF volume.

**API details used** (from the official `fyers-apiv3` SDK 3.2.0 source and the
official historical-data docs summary): `GET https://api-t1.fyers.in/data/history`,
header `Authorization: <app_id>:<access_token>` plus `version: 3`, parameters
`symbol`, `resolution=5`, `date_format=0` (epoch), `range_from`, `range_to`,
`cont_flag=1`; candles are `[epoch, open, high, low, close, volume]`. The FYERS
documentation pages themselves could not be opened from the development
environment, so the error-code table, numeric rate limits, token lifetime and how
far back 5-minute history goes were **not verified**: the pacing defaults are
conservative and error classification uses the HTTP status plus the `s`/`code`/
`message` fields on a best-effort basis. Check
<https://fyers.in/api-docs/v3/api/market-data/historical-data/> and the
rate-limit page before relying on long downloads.

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
* `build_feature_frame` validates its input and rejects unordered or duplicated
  candles; `compute_cpr` refuses unordered candles (a swapped pair inside a
  session would otherwise silently change the next session's CPR).
* Signal rules only see the explicit `pipeline.FEATURE_COLUMNS` allow-list, so
  extra columns in a research frame (e.g. forward returns) cannot leak into rules.
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
(`NIFTY_SM_ALERT_TTL_BARS`), capped at the 15:30 close unless
`NIFTY_SM_EXPIRE_AT_SESSION_END=false`. The machine tracks one setup at a time.

## Configuration

All settings are environment variables with the `NIFTY_` prefix; see
`.env.example` for the full list and defaults. The engine does not auto-load
`.env` files (only the FYERS downloader reads its two `FYERS_*` credentials from one). Invalid values (non-numeric, non-finite, negative thresholds, or a
minimum-RVOL rule without a traded-volume source) raise `ConfigError` at start-up.

## Credentials

Only the optional FYERS downloader uses credentials (`FYERS_APP_ID`,
`FYERS_ACCESS_TOKEN`); the signal engine itself needs none. They must be supplied via
environment variables or a local `.env`, never committed or logged. `.env`, key
files and token files are git-ignored.

## Assumptions and limitations

* No NSE holiday calendar: "previous session" means the previous session
  present in the data. Gaps > 4 calendar days are logged.
* Daily H/L/C for CPR are derived from the 5-minute candles by default; NSE's
  official close can differ. Pass official values via `compute_cpr(daily=...)`.
* Moving averages run continuously across sessions (no daily reset).
* A previous session with fewer than 75 candles (half-days, special sessions, data
  holes) gives NaN CPR, and therefore UNKNOWN regime, for the following session.
* Moving averages and the regime slope run across the overnight gap; rolling RVOL
  compares the open with the previous afternoon; time-of-day RVOL counts
  observations per slot, so a missing candle widens its calendar lookback.
  Futures contract rollovers are not handled.
* Processing is batch (whole frame). A live feed would need incremental feature
  computation; recomputing on every candle is O(n) per candle.
* Tested on Python 3.13 with pandas 2.2.3/numpy 2.1.3 and pandas 3.0.6/numpy 2.5.4.
  Python 3.11/3.12 and numpy 1.26 (declared lower bounds) are untested. No CI yet.
* Sample data is synthetic and says nothing about real market behaviour.
