"""
Qobuz authentication module.

Handles credential scraping, API authentication, and token management.
"""

from .api_client import QobuzAPIClient, QobuzAPIError
from .credentials import (
    auto_fetch_credentials,
    load_cached_credentials,
    save_credentials_to_cache,
)
from .exceptions import AuthenticationError
from .tokens import QobuzToken, WSToken

__all__ = [
    "auto_fetch_credentials",
    "load_cached_credentials",
    "save_credentials_to_cache",
    "QobuzAPIClient",
    "QobuzAPIError",
    "AuthenticationError",
    "QobuzToken",
    "WSToken",
]
