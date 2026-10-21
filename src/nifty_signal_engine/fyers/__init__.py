"""Local-only FYERS tooling: historical candle downloader and login helper.

``download_nifty_5m`` (REST, read-only) saves completed 5-minute NIFTY 50 candles to local
CSV files. ``fyers.login`` runs the interactive Individual App login on a loopback callback
and stores only the access token in ``.env``. Nothing here opens streaming connections,
places orders, or sends notifications.
Credentials come from environment variables or a git-ignored ``.env`` file and are
never logged, stored in output files, or included in error messages.
"""

from .auth import REDIRECT_URI, CallbackServer, exchange_auth_code, generate_state
from .client import FyersHistoryClient, HttpResponse, urllib_transport
from .credentials import (
    AuthCredentials,
    Credentials,
    SecretStr,
    load_auth_credentials,
    load_credentials,
    parse_dotenv,
)
from .downloader import (
    MAX_WINDOW_DAYS,
    NIFTY_SYMBOL,
    DownloadResult,
    download_nifty_5m,
    split_date_range,
)
from .errors import (
    AuthFlowError,
    CredentialsError,
    EnvFileError,
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
    "REDIRECT_URI",
    "AuthCredentials",
    "AuthFlowError",
    "CallbackServer",
    "Credentials",
    "CredentialsError",
    "DownloadResult",
    "EnvFileError",
    "FyersApiError",
    "FyersAuthError",
    "FyersError",
    "FyersHistoryClient",
    "FyersNetworkError",
    "FyersRateLimitError",
    "HttpResponse",
    "MissingCredentialsError",
    "NoDataError",
    "SecretStr",
    "download_nifty_5m",
    "exchange_auth_code",
    "generate_state",
    "load_auth_credentials",
    "load_credentials",
    "parse_dotenv",
    "split_date_range",
    "urllib_transport",
]
