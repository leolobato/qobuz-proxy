"""
Authentication exceptions.
"""


class AuthenticationError(Exception):
    """Raised when Qobuz authentication fails."""

    pass


class TransientAuthError(Exception):
    """Raised when token validation fails for a reason that isn't a bad token.

    Covers timeouts, connection errors, and non-2xx/401/403 responses from
    Qobuz — situations where retrying shortly after is likely to succeed,
    as opposed to a definitive 401/403 (real invalid/revoked token).
    """

    pass
