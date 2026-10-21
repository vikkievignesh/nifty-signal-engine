"""Command line: ``python -m nifty_signal_engine --csv path``.

Loads a CSV, builds features and regimes, and runs the UNVALIDATED example rules.
Prints research output only; no orders, no network access.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import Settings
from .data_loader import load_candles_csv
from .logging_config import configure_logging
from .pipeline import build_example_machine, build_feature_frame
from .signals.runner import run_signals

logger = logging.getLogger("nifty_signal_engine")


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="nifty_signal_engine", description=__doc__)
    parser.add_argument("--csv", type=Path, default=settings.data_dir / "sample" / "synthetic_nifty_5m.csv")
    args = parser.parse_args(argv)

    configure_logging(settings.log_level)
    candles = load_candles_csv(args.csv)
    frame = build_feature_frame(candles, settings)
    result = run_signals(frame, build_example_machine(settings))

    print("RESEARCH OUTPUT — example rules are UNVALIDATED; not trading advice.")
    print(f"candles: {len(frame)}  sessions: {frame['timestamp'].dt.normalize().nunique()}")
    print("regime counts:")
    print(frame["regime"].value_counts().to_string())
    print(f"setup transitions: {len(result.transitions)}  alerts: {len(result.alerts)}")
    if result.alerts:
        cols = ["alert_id", "direction", "level_name", "level", "available_at", "expires_at", "regime"]
        print(result.alerts_frame()[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
