"""
Shared types for Qobuz Connect protocol.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class JWTConnectToken:
    """WebSocket JWT token received from Qobuz app."""

    jwt: str = ""
    exp: int = 0  # Expiration timestamp in seconds (UTC)
    endpoint: str = ""  # WebSocket endpoint URL

    def is_valid(self) -> bool:
        """Check if token has required fields."""
        return bool(self.jwt and self.exp and self.endpoint)


@dataclass
class JWTApiToken:
    """API JWT token received from Qobuz app."""

    jwt: str = ""
    exp: int = 0  # Expiration timestamp in seconds (UTC)

    def is_valid(self) -> bool:
        """Check if token has required fields."""
        return bool(self.jwt and self.exp)


@dataclass
class ConnectTokens:
    """Tokens received from POST /connect-to-qconnect."""

    session_id: str = ""
    ws_token: Optional[JWTConnectToken] = None
    api_token: Optional[JWTApiToken] = None

    def is_valid(self) -> bool:
        """Check if all required tokens are present."""
        return bool(self.session_id and self.ws_token and self.ws_token.is_valid())
