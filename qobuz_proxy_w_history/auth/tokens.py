"""
Token management for Qobuz authentication.
"""

import time
from dataclasses import dataclass


@dataclass
class QobuzToken:
    """API token with expiration."""

    token: str = ""
    expires_at: int = 0  # Milliseconds

    def is_expired(self, buffer_ms: int = 60000) -> bool:
        """Check if token is expired or will expire within buffer."""
        if not self.token or not self.expires_at:
            return True
        now_ms = int(time.time() * 1000)
        return now_ms + buffer_ms >= self.expires_at


@dataclass
class WSToken:
    """WebSocket authentication token (received from Qobuz app)."""

    jwt: str = ""
    exp_s: int = 0  # Expiration in seconds (UTC)
    endpoint: str = ""  # WebSocket endpoint URL

    def is_expired(self, buffer_s: int = 60) -> bool:
        """Check if token is expired or will expire within buffer."""
        if not self.jwt or not self.exp_s:
            return True
        now_s = int(time.time())
        return now_s + buffer_s >= self.exp_s

    def is_valid(self) -> bool:
        """Check if token has all required fields."""
        return bool(self.jwt and self.exp_s and self.endpoint)

    @classmethod
    def from_connect_token(cls, jwt: str, exp: int, endpoint: str) -> "WSToken":
        """Create from connect request data."""
        return cls(jwt=jwt, exp_s=exp, endpoint=endpoint)
