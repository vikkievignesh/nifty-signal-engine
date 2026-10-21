"""Local-only FYERS historical candle downloader (REST, read-only).

Downloads completed 5-minute candles for the NIFTY 50 index into local CSV files.
It never opens streaming connections, places orders, or sends notifications.
Credentials come from environment variables or a git-ignored ``.env`` file and are
never logged, stored in output files, or included in error messages.
"""

from .client import FyersHistoryClient, HttpResponse, urllib_transport
from .credentials import Credentials, load_credentials, parse_dotenv
from .downloader import (
    MAX_WINDOW_DAYS,
    NIFTY_SYMBOL,
    DownloadResult,
    download_nifty_5m,
    split_date_range,
)
from .errors import (
    CredentialsError,
    FyersApiError,
    FyersAuthError,
    FyersError,
    FyersNetworkError,
    FyersRateLimitError,
    MissingCredentialsError,
    NoDataError,
)

__all__ = [
    "MAX_WINDOW_DAYS",
    "NIFTY_SYMBOL",
    "Credentials",
    "CredentialsError",
    "DownloadResult",
    "FyersApiError",
    "FyersAuthError",
    "FyersError",
    "FyersHistoryClient",
    "FyersNetworkError",
    "FyersRateLimitError",
    "HttpResponse",
    "MissingCredentialsError",
    "NoDataError",
    "download_nifty_5m",
    "load_credentials",
    "parse_dotenv",
    "split_date_range",
    "urllib_transport",
]
