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

        mock_session = _make_mock_session(503, text_body="Service Unavailable")

        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(TransientAuthError):
                await client.login_with_token("999", "token")

        assert client.user_auth_token is None


def _make_mock_session(status: int, json_body: dict | None = None, text_body: str = ""):
    """Build an aiohttp.ClientSession mock returning one canned response."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_body)
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestGetWSToken:
    def _client(self) -> QobuzAPIClient:
        client = QobuzAPIClient("app123", "secret456")
        client.user_auth_token = "user_token"
        client.user_id = "999"
        # Pre-seed a valid session so start_session() returns early
        client.x_session_id = "session123"
        client.x_session_expires_at = 2**53
        return client

    async def test_mints_token_via_create_token(self):
        client = self._client()
        mock_session = _make_mock_session(
            200,
            {
                "jwt_qws": {
                    "jwt": "ws_jwt_value",
                    "exp": 9999999999,
                    "endpoint": "wss%3A%2F%2Fqws-us-prod.qobuz.com%2Fws",
                }
            },
        )

        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            token = await client.get_ws_token()

        assert token is not None
        assert token.jwt == "ws_jwt_value"
        assert token.exp_s == 9999999999
        assert token.endpoint == "wss://qws-us-prod.qobuz.com/ws"

        call = mock_session.post.call_args
        assert call.args[0].endswith("/qws/createToken")
        assert call.kwargs["data"] == "jwt=jwt_qws"
        headers = call.kwargs["headers"]
        assert headers["X-App-Id"] == "app123"
        assert headers["X-User-Auth-Token"] == "user_token"
        assert headers["X-Session-Id"] == "session123"

    async def test_second_mint_uses_refresh_token(self):
        client = self._client()
        json_body = {"jwt_qws": {"jwt": "ws_jwt", "exp": 9999999999, "endpoint": "wss://x/ws"}}

        with patch(
            "qobuz_proxy.auth.api_client.aiohttp.ClientSession",
            return_value=_make_mock_session(200, json_body),
        ):
            assert await client.get_ws_token() is not None

        mock_session = _make_mock_session(200, json_body)
        with patch("qobuz_proxy.auth.api_client.aiohttp.ClientSession", return_value=mock_session):
            assert await client.get_ws_token() is not None

        assert mock_session.post.call_args.args[0].endswith("/qws/refreshToken")

    async def test_http_error_returns_none(self):
        client = self._client()

        with patch(
            "qobuz_proxy.auth.api_client.aiohttp.ClientSession",
            return_value=_make_mock_session(401, text_body="Unauthorized"),
        ):
            assert await client.get_ws_token() is None

    async def test_incomplete_token_returns_none(self):
        client = self._client()

        with patch(
            "qobuz_proxy.auth.api_client.aiohttp.ClientSession",
            return_value=_make_mock_session(200, {"jwt_qws": {"jwt": "only_jwt"}}),
        ):
            assert await client.get_ws_token() is None

    async def test_no_auth_token_returns_none(self):
        client = QobuzAPIClient("app123", "secret456")

        token = await client.get_ws_token()

        assert token is None
