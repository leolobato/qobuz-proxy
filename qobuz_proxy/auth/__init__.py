"""
Qobuz authentication module.

Handles OAuth authentication, API signing, and token management.
"""

from .api_client import QobuzAPIClient, QobuzAPIError
from .credentials import (
    clear_user_token,
    load_user_token,
    save_user_token,
)
from .exceptions import AuthenticationError, TransientAuthError
from .tokens import QobuzToken, WSToken

__all__ = [
    "clear_user_token",
    "load_user_token",
    "save_user_token",
    "QobuzAPIClient",
    "QobuzAPIError",
    "AuthenticationError",
    "TransientAuthError",
    "QobuzToken",
    "WSToken",
]
