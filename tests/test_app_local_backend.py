"""Tests for app.py local backend integration (QPROXY-023)."""

from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.config import Config, AUTO_QUALITY


def _make_local_config() -> Config:
    """Create a config with local backend."""
    config = Config()
    config.qobuz.email = "test@example.com"
    config.qobuz.password = "testpass"
    config.backend.type = "local"
    config.backend.local.device = "default"
    config.backend.local.buffer_size = 2048
    return config


def _mock_sounddevice():
    sd = MagicMock()
    sd.query_devices.return_value = [
        {
            "name": "Test Output",
            "max_output_channels": 2,
            "max_input_channels": 0,
            "default_samplerate": 44100.0,
        },
    ]
    sd.default.device = (0, 0)
    return sd


_SD_PATCH = "qobuz_proxy.backends.local.device._import_sounddevice"


class TestAppLocalBackend:
    async def test_app_creates_local_backend(self) -> None:
        """App should create LocalAudioBackend when config type is 'local'."""
        config = _make_local_config()
        app = QobuzProxy(config)

        # Mock everything before backend creation
        with (
            patch(
                "qobuz_proxy.app.auto_fetch_credentials",
                new_callable=AsyncMock,
                return_value={"app_id": "test-id", "app_secret": "test-secret"},
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as mock_api_class,
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
        ):
            mock_api = mock_api_class.return_value
            mock_api.login = AsyncMock(return_value=True)

            # Start will create backend and continue... we need to stop it
            # after backend creation. We'll patch DiscoveryService to raise
            # and catch that.
            with patch("qobuz_proxy.app.DiscoveryService") as mock_disc:
                mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))

                try:
                    await app.start()
                except Exception:
                    pass

        assert isinstance(app._backend, LocalAudioBackend)

    async def test_app_skips_proxy_for_local(self) -> None:
        """No proxy server should be created for local backend."""
        config = _make_local_config()
        app = QobuzProxy(config)

        with (
            patch(
                "qobuz_proxy.app.auto_fetch_credentials",
                new_callable=AsyncMock,
                return_value={"app_id": "test-id", "app_secret": "test-secret"},
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as mock_api_class,
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
        ):
            mock_api = mock_api_class.return_value
            mock_api.login = AsyncMock(return_value=True)

            with patch("qobuz_proxy.app.DiscoveryService") as mock_disc:
                mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))

                try:
                    await app.start()
                except Exception:
                    pass

        assert app._proxy_server is None

    async def test_app_quality_defaults_hires_for_local(self) -> None:
        """Auto quality with local backend should default to 27 (Hi-Res 192k)."""
        config = _make_local_config()
        config.qobuz.max_quality = AUTO_QUALITY
        app = QobuzProxy(config)

        with (
            patch(
                "qobuz_proxy.app.auto_fetch_credentials",
                new_callable=AsyncMock,
                return_value={"app_id": "test-id", "app_secret": "test-secret"},
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as mock_api_class,
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
        ):
            mock_api = mock_api_class.return_value
            mock_api.login = AsyncMock(return_value=True)

            with patch("qobuz_proxy.app.DiscoveryService") as mock_disc:
                mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))

                try:
                    await app.start()
                except Exception:
                    pass

        assert app._effective_quality == 27

    async def test_app_skips_fixed_volume_for_local(self) -> None:
        """Fixed volume should not be applied for local backend."""
        config = _make_local_config()
        config.backend.dlna.fixed_volume = True  # Set but shouldn't be applied
        app = QobuzProxy(config)

        with (
            patch(
                "qobuz_proxy.app.auto_fetch_credentials",
                new_callable=AsyncMock,
                return_value={"app_id": "test-id", "app_secret": "test-secret"},
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as mock_api_class,
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
            patch("qobuz_proxy.app.QobuzPlayer") as mock_player_class,
        ):
            mock_api = mock_api_class.return_value
            mock_api.login = AsyncMock(return_value=True)

            mock_player = mock_player_class.return_value

            with patch("qobuz_proxy.app.DiscoveryService") as mock_disc:
                mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))

                try:
                    await app.start()
                except Exception:
                    pass

        # set_fixed_volume_mode should NOT have been called
        mock_player.set_fixed_volume_mode.assert_not_called()
