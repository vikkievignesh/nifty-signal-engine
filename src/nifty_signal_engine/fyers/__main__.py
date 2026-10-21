"""Command line: ``python -m nifty_signal_engine.fyers --start YYYY-MM-DD [--end YYYY-MM-DD]``.

Downloads NIFTY 50 five-minute candles to ``<NIFTY_DATA_DIR>/local/fyers`` (git-ignored
by default). Needs ``FYERS_APP_ID`` and ``FYERS_ACCESS_TOKEN`` in the environment or a
local ``.env``. Read-only: no streaming, no orders, no notifications.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

from ..config import ConfigError, Settings
from ..logging_config import configure_logging
from ..schema import IST
from ..validation import CandleValidationError
from .client import FyersHistoryClient, Transport, urllib_transport, validate_user_agent
from .credentials import load_credentials
from .downloader import DEFAULT_WINDOW_DAYS, MAX_WINDOW_DAYS, NIFTY_SYMBOL, download_nifty_5m
from .errors import FyersError

logger = logging.getLogger("nifty_signal_engine.fyers")


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] | None = None,
    now: datetime | None = None,
    dotenv_path: str | Path = ".env",
) -> int:
    parser = argparse.ArgumentParser(prog="python -m nifty_signal_engine.fyers", description=__doc__)
    parser.add_argument("--start", type=_iso_date, required=True, help="first date, YYYY-MM-DD (IST)")
    parser.add_argument("--end", type=_iso_date, default=None, help="last date, YYYY-MM-DD (default: today IST)")
    parser.add_argument("--out-dir", type=Path, default=None, help="default: <NIFTY_DATA_DIR>/local/fyers")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"calendar days per request, 1..{MAX_WINDOW_DAYS} (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--min-interval", type=float, default=1.0, help="minimum seconds between requests")
    parser.add_argument("--no-raw", action="store_true", help="do not keep raw API responses")
    parser.add_argument("--user-agent", type=str, default=None, metavar="VALUE",
                        help="EXPERIMENTAL, diagnostic only: send this User-Agent with the history requests "
                             "(printable ASCII, max 200 chars). Default: unchanged (urllib's own).")
    parser.add_argument("--allow-off-hours", action="store_true",
                        help="accept candles outside 09:15-15:25 (e.g. special sessions)")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env(env)
        configure_logging(settings.log_level)
        credentials = load_credentials(env, dotenv_path)
        client_kwargs = {"sleep": sleep} if sleep else {}
        user_agent = validate_user_agent(args.user_agent) if args.user_agent is not None else None
        client = FyersHistoryClient(credentials, transport=transport, min_interval_seconds=args.min_interval,
                                    user_agent=user_agent, **client_kwargs)  # nothing is sent before this
        if user_agent is not None:
            print("NOTE: EXPERIMENTAL User-Agent override is active for the history requests (diagnostic only; "
                  f"it is not a fix): {user_agent}")
        end = args.end or (now or datetime.now(IST)).astimezone(IST).date()
        result = download_nifty_5m(
            args.start, end, client=client,
            out_dir=args.out_dir or settings.data_dir / "local" / "fyers",
            window_days=args.window_days, save_raw=not args.no_raw,
            allow_off_hours=args.allow_off_hours, now=now, symbol=NIFTY_SYMBOL,
        )
    except (FyersError, CandleValidationError, ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # all messages are credential-free by construction
        return 1

    meta = result.metadata
    print(f"wrote {meta['rows']} candles ({meta['first_candle']} .. {meta['last_candle']}) to {result.csv_path}")
    print(f"metadata: {result.meta_path}")
    if meta["weekdays_without_data"] or meta["incomplete_sessions"]:
        print(f"note: {len(meta['weekdays_without_data'])} weekday(s) without data (holidays or gaps), "
              f"{len(meta['incomplete_sessions'])} incomplete session(s); see metadata")
    print("WARNING: volume is spot-index volume and must not be treated as traded volume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
