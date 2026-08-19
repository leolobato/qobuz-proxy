"""Integration tests for QobuzProxy OAuth auth startup flow."""

from unittest.mock import AsyncMock, patch

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.auth.exceptions import TransientAuthError
from qobuz_proxy.config import (
    Config,
    QobuzConfig,
    ServerConfig,
    LoggingConfig,
    SpeakerConfig,
)


def _make_config(**overrides) -> Config:
    config = Config()
    config.qobuz = QobuzConfig()
    config.backend.type = "stub"
    config.server = ServerConfig(http_port=0, bind_address="127.0.0.1")
    config.logging = LoggingConfig(level="warning")
    config.speakers = [
        SpeakerConfig(
            name="Test Speaker",
            uuid="test-uuid",
            backend_type="stub",
            http_port=0,
            bind_address="127.0.0.1",
        )
    ]
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


class TestStartupWithoutToken:
    async def test_web_server_starts(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch("qobuz_proxy.app.load_user_token", return_value=None),
        ):
            try:
                await app.start()
                assert app._web_app is not None
            finally:
                await app.stop()

    async def test_auth_state_is_unauthenticated(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch("qobuz_proxy.app.load_user_token", return_value=None),
        ):
            try:
                await app.start()
                assert app._auth_state["authenticated"] is False
            finally:
                await app.stop()


class TestStartupWithCachedToken:
    async def test_authenticate_called_with_cached_token(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={"user_id": "999", "user_auth_token": "tok"},
            ),
            patch.object(
                app, "_authenticate", new_callable=AsyncMock, return_value=True
            ) as mock_auth,
            patch.object(app, "_start_speakers", new_callable=AsyncMock),
        ):
            try:
                await app.start()
                mock_auth.assert_awaited_once_with("999", "tok")
            finally:
                await app.stop()


class TestAuthenticateRetriesTransientErrors:
    """Regression tests for the missing retry on token validation at startup.

    Previously, any exception from login_with_token (timeouts included —
    asyncio.TimeoutError stringifies to '') was indistinguishable from a
    genuinely bad token: one slow response from Qobuz right after boot
    permanently required a manual re-auth via the web UI.
    """

    async def test_retries_and_succeeds_after_transient_errors(self):
        config = _make_config()
        app = QobuzProxy(config)
        # Normally set by start() (step 2, before auto-auth); tests call
        # _authenticate() directly so it needs setting explicitly.
        app._app_id = "test_app_id"
        app._app_secret = "test_app_secret"
        with (
            patch("qobuz_proxy.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(
                "qobuz_proxy.app.QobuzAPIClient.login_with_token",
                new_callable=AsyncMock,
                side_effect=[
                    TransientAuthError("timeout"),
                    TransientAuthError("timeout"),
                    True,
                ],
            ),
        ):
            result = await app._authenticate("999", "tok")

        assert result is True
        assert mock_sleep.await_count == 2

    async def test_gives_up_after_max_attempts_of_transient_errors(self):
        config = _make_config()
        app = QobuzProxy(config)
        app._app_id = "test_app_id"
        app._app_secret = "test_app_secret"
        with (
            patch("qobuz_proxy.app.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "qobuz_proxy.app.QobuzAPIClient.login_with_token",
                new_callable=AsyncMock,
                side_effect=TransientAuthError("timeout"),
            ) as mock_login,
        ):
            result = await app._authenticate("999", "tok")

        assert result is False
        assert app._api_client is None
        assert mock_login.await_count == 3  # 1 initial + 2 retries

    async def test_does_not_retry_on_definitive_bad_token(self):
        """A real 401/403 (login_with_token returns False) should fail fast —
        no point retrying a token that's actually invalid."""
        config = _make_config()
        app = QobuzProxy(config)
        # Normally set by start() (step 2, before auto-auth); tests call
        # _authenticate() directly so it needs setting explicitly.
        app._app_id = "test_app_id"
        app._app_secret = "test_app_secret"
        with (
            patch("qobuz_proxy.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(
                "qobuz_proxy.app.QobuzAPIClient.login_with_token",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_login,
        ):
            result = await app._authenticate("999", "bad_tok")

        assert result is False
        assert mock_login.await_count == 1
        mock_sleep.assert_not_awaited()


class TestWebUIAuthCallback:
    async def test_callback_sets_up_api_client(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.save_user_token"),
            patch.object(app, "_start_speakers", new_callable=AsyncMock),
        ):
            try:
                await app.start()
                result = await app._on_auth_token("999", "tok")
                assert result is True
                assert app._api_client is not None
                assert app._api_client.user_auth_token == "tok"
                assert app._api_client.user_id == "999"
            finally:
                await app.stop()

    async def test_callback_saves_token(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.save_user_token") as mock_save,
            patch.object(app, "_start_speakers", new_callable=AsyncMock),
        ):
            try:
                await app.start()
                await app._on_auth_token("999", "tok")
                mock_save.assert_called_once_with(user_id="999", auth_token="tok", email="")
            finally:
                await app.stop()

    async def test_callback_starts_speakers(self):
        config = _make_config()
        app = QobuzProxy(config)
        with (
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.save_user_token"),
            patch.object(app, "_start_speakers", new_callable=AsyncMock) as mock_start,
        ):
            try:
                await app.start()
                await app._on_auth_token("999", "tok")
                mock_start.assert_awaited_once()
            finally:
                await app.stop()
