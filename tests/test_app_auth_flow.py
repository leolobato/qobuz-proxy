"""Integration tests for QobuzProxy OAuth auth startup flow."""

import asyncio
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


class TestStartupTransientAuthErrors:
    """Regression tests for startup token validation when Qobuz is unreachable.

    Previously, any exception from login_with_token (timeouts included —
    asyncio.TimeoutError stringifies to '') was indistinguishable from a
    genuinely bad token: one slow response from Qobuz right after boot
    permanently required a manual re-auth via the web UI.
    """

    def _start_patches(self, login_side_effect, delays=(0.0,)):
        return (
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={"user_id": "999", "user_auth_token": "tok", "email": "a@b.c"},
            ),
            patch("qobuz_proxy.app.AUTH_RETRY_DELAYS_SECONDS", delays),
            patch("qobuz_proxy.app.AUTH_RETRY_STEADY_DELAY_SECONDS", delays[-1]),
            patch(
                "qobuz_proxy.app.QobuzAPIClient.login_with_token",
                new_callable=AsyncMock,
                side_effect=login_side_effect,
            ),
        )

    async def test_retries_in_background_until_qobuz_answers(self):
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches(
            [TransientAuthError("timeout"), TransientAuthError("timeout"), True]
        )
        with (
            load,
            delays,
            steady,
            login as mock_login,
            patch.object(app, "_start_speakers", new_callable=AsyncMock) as mock_start,
        ):
            try:
                await app.start()

                # Startup completes without blocking on the retries
                assert app._auth_state["authenticated"] is False
                assert app._auth_retry_task is not None
                mock_start.assert_not_awaited()

                await app._auth_retry_task

                assert app._auth_state["authenticated"] is True
                assert app._auth_state["user_id"] == "999"
                assert app._auth_state["email"] == "a@b.c"
                assert app._api_client is not None
                assert mock_login.await_count == 3
                mock_start.assert_awaited_once()
                assert app._auth_retry_task is None
            finally:
                await app.stop()

    async def test_background_retry_stops_when_qobuz_rejects_token(self):
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches([TransientAuthError("timeout"), False])
        with (
            load,
            delays,
            steady,
            login as mock_login,
            patch.object(app, "_start_speakers", new_callable=AsyncMock) as mock_start,
        ):
            try:
                await app.start()
                task = app._auth_retry_task
                assert task is not None
                await task

                assert app._auth_state["authenticated"] is False
                assert app._api_client is None
                assert app._auth_retry_task is None
                assert mock_login.await_count == 2
                mock_start.assert_not_awaited()
            finally:
                await app.stop()

    async def test_rejected_token_at_startup_does_not_retry(self):
        """A real 401/403 (login_with_token returns False) should fail fast —
        no point retrying a token that's actually invalid."""
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches([False])
        with (
            load,
            delays,
            steady,
            login as mock_login,
            patch.object(app, "_start_speakers", new_callable=AsyncMock),
        ):
            try:
                await app.start()

                assert app._auth_state["authenticated"] is False
                assert app._auth_retry_task is None
                assert app._api_client is None
                assert mock_login.await_count == 1
            finally:
                await app.stop()

    async def test_web_ui_login_cancels_background_retry(self):
        """A fresh login supersedes the pending validation of the old saved
        token, so a late result from the retry can't clobber the new client."""
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches(
            TransientAuthError("timeout"), delays=(60.0,)
        )
        with (
            load,
            delays,
            steady,
            login as mock_login,
            patch("qobuz_proxy.app.save_user_token"),
            patch.object(app, "_start_speakers", new_callable=AsyncMock) as mock_start,
        ):
            try:
                await app.start()
                task = app._auth_retry_task
                assert task is not None

                await app._on_auth_token("999", "new_tok")

                assert task.cancelled()
                assert app._auth_retry_task is None
                assert app._auth_state["authenticated"] is True
                assert app._api_client is not None
                assert app._api_client.user_auth_token == "new_tok"
                assert mock_login.await_count == 1
                mock_start.assert_awaited_once()
            finally:
                await app.stop()

    async def test_web_ui_login_during_initial_validation_wins(self):
        """A login submitted while the very first validation is still waiting on
        Qobuz must not be overwritten when that validation completes later."""
        app = QobuzProxy(_make_config())
        validation_started = asyncio.Event()

        async def slow_login(**_kwargs):
            validation_started.set()
            await asyncio.Event().wait()  # Qobuz never answers

        load, delays, steady, login = self._start_patches(slow_login)
        with (
            load,
            delays,
            steady,
            login as mock_login,
            patch("qobuz_proxy.app.save_user_token"),
            patch.object(app, "_start_speakers", new_callable=AsyncMock) as mock_start,
        ):
            try:
                start_task = asyncio.create_task(app.start())
                await validation_started.wait()
                task = app._auth_retry_task
                assert task is not None

                await app._on_auth_token("999", "new_tok")
                await start_task

                assert task.cancelled()
                assert app._auth_retry_task is None
                assert app._auth_state["authenticated"] is True
                assert app._api_client is not None
                assert app._api_client.user_auth_token == "new_tok"
                assert mock_login.await_count == 1
                mock_start.assert_awaited_once()
            finally:
                await app.stop()

    async def test_logout_cancels_background_retry(self):
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches(
            TransientAuthError("timeout"), delays=(60.0,)
        )
        with (
            load,
            delays,
            steady,
            login,
            patch("qobuz_proxy.app.clear_user_token"),
        ):
            try:
                await app.start()
                task = app._auth_retry_task
                assert task is not None

                await app._on_logout()

                assert task.cancelled()
                assert app._auth_retry_task is None
            finally:
                await app.stop()

    async def test_stop_cancels_background_retry(self):
        app = QobuzProxy(_make_config())
        load, delays, steady, login = self._start_patches(
            TransientAuthError("timeout"), delays=(60.0,)
        )
        with load, delays, steady, login:
            await app.start()
            task = app._auth_retry_task
            assert task is not None

            await app.stop()

            assert task.cancelled()
            assert app._auth_retry_task is None


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
