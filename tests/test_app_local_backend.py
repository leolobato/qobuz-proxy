"""Tests for Speaker local backend integration (QPROXY-023)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.config import AUTO_QUALITY, SpeakerConfig
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.speaker import Speaker


def _make_local_speaker_config(**overrides) -> SpeakerConfig:
    """Create a SpeakerConfig configured for the local backend."""
    cfg = SpeakerConfig(
        name="TestSpeaker",
        uuid=str(uuid.uuid4()),
        backend_type="local",
        audio_device="default",
        audio_buffer_size=2048,
        http_port=0,
        proxy_port=0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


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


def _make_speaker(config: SpeakerConfig) -> Speaker:
    return Speaker(config=config, api_client=MagicMock(), app_id="test-app-id")


class TestSpeakerLocalBackend:
    async def test_speaker_creates_local_backend(self) -> None:
        """Speaker should create LocalAudioBackend when config.backend_type is 'local'."""
        speaker = _make_speaker(_make_local_speaker_config())

        with (
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
            # Stop start() right after backend/player are created but before networking.
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc,
        ):
            mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))
            await speaker.start()

        assert isinstance(speaker._backend, LocalAudioBackend)

    async def test_speaker_skips_proxy_for_local(self) -> None:
        """No DLNA proxy server should be created for the local backend."""
        speaker = _make_speaker(_make_local_speaker_config())

        with (
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc,
        ):
            mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))
            await speaker.start()

        assert speaker._proxy_server is None

    async def test_speaker_quality_defaults_hires_for_local(self) -> None:
        """Auto quality with the local backend should resolve to 27 (Hi-Res 192k)."""
        speaker = _make_speaker(_make_local_speaker_config(max_quality=AUTO_QUALITY))

        with (
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc,
        ):
            mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))
            await speaker.start()

        assert speaker._effective_quality == 27

    async def test_speaker_skips_fixed_volume_for_local(self) -> None:
        """Fixed volume mode should not be applied for the local backend."""
        # dlna_fixed_volume is set, but local backend must ignore it.
        speaker = _make_speaker(_make_local_speaker_config(dlna_fixed_volume=True))

        with (
            patch(_SD_PATCH, return_value=_mock_sounddevice()),
            patch("qobuz_proxy.speaker.QobuzPlayer") as mock_player_class,
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc,
        ):
            mock_disc.return_value.start = AsyncMock(side_effect=Exception("stop here"))
            mock_player = mock_player_class.return_value
            await speaker.start()

        mock_player.set_fixed_volume_mode.assert_not_called()

    async def test_setup_websocket_reuses_existing_manager(self) -> None:
        """Fresh handshakes should refresh tokens on the existing manager, not rebuild it."""
        speaker = _make_speaker(_make_local_speaker_config())
        speaker._queue = MagicMock()
        speaker._player = MagicMock()
        speaker._ws_manager = MagicMock()

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="refreshed_jwt_token",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        with patch("qobuz_proxy.speaker.WsManager") as mock_ws_manager_class:
            await speaker._setup_websocket(tokens)

        speaker._ws_manager.set_tokens.assert_called_once_with(tokens, activate=True)
        assert speaker._ws_connected_event.is_set() is True
        mock_ws_manager_class.assert_not_called()
