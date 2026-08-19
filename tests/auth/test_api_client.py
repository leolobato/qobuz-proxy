from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.auth.api_client import QobuzAPIClient
from qobuz_proxy.auth.exceptions import TransientAuthError


class TestLoginWithToken:
    async def test_successful_login(self):
        client = QobuzAPIClient("app123", "secret456")
        mock_response_json = {
            "user_auth_token": "fresh_token",
            "user": {"id": 999, "email": "test@example.com"},
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_response_json)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.login_with_token("999", "old_token")

        assert result is True
        assert client.user_auth_token == "fresh_token"
        assert client.user_id == "999"

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert call_kwargs.kwargs["data"] == "extra=partner"
        headers = call_kwargs.kwargs["headers"]
        assert headers["X-App-Id"] == "app123"
        assert headers["X-User-Auth-Token"] == "old_token"

    async def test_failed_login_returns_false(self):
        client = QobuzAPIClient("app123", "secret456")

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="Unauthorized")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.login_with_token("999", "bad_token")

        assert result is False
        assert client.user_auth_token is None

    async def test_login_network_error_raises_transient_auth_error(self):
        """A timeout/connection error means "unknown", not "bad token" — the
        caller (QobuzProxy._authenticate) needs to distinguish the two to
        retry instead of demanding a fresh manual login."""
        client = QobuzAPIClient("app123", "secret456")

        with patch(
            "qobuz_proxy.auth.api_client.aiohttp.ClientSession",
            side_effect=Exception("network error"),
        ):
            with pytest.raises(TransientAuthError):
                await client.login_with_token("999", "token")

        assert client.user_auth_token is None

    async def test_login_server_error_raises_transient_auth_error(self):
        client = QobuzAPIClient("app123", "secret456")

        mock_resp = AsyncMock()
        mock_resp.status = 503
        mock_resp.text = AsyncMock(return_value="Service Unavailable")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(TransientAuthError):
                await client.login_with_token("999", "token")

        assert client.user_auth_token is None
