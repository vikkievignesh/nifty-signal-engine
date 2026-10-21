"""Logging setup (stdlib only)."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once; safe to call repeatedly."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, force=True)
