"""Tests for the Speaker class."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.config import (
    AUTO_QUALITY,
    Config,
    SpeakerConfig,
)
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.speaker import Speaker


def _make_speaker_config(**kwargs) -> SpeakerConfig:
    """Create a minimal SpeakerConfig suitable for testing."""
    defaults = dict(
        name="Test Speaker",
        uuid="test-uuid-1234",
        backend_type="dlna",
        max_quality=27,
        http_port=8689,
        bind_address="0.0.0.0",
        dlna_ip="192.168.1.100",
        dlna_port=1400,
        dlna_fixed_volume=False,
        proxy_port=7120,
        audio_device="default",
        audio_buffer_size=2048,
    )
    defaults.update(kwargs)
    return SpeakerConfig(**defaults)


def _make_api_client() -> MagicMock:
    return MagicMock()


class TestSpeakerConstruction:
    def test_creates_with_config(self):
        config = _make_speaker_config(name="Living Room")
        api_client = _make_api_client()
        speaker = Speaker(config=config, api_client=api_client, app_id="my-app-id")

        assert speaker._config is config
        assert speaker._api_client is api_client
        assert speaker._app_id == "my-app-id"
        assert speaker._is_running is False

    def test_name_property(self):
        config = _make_speaker_config(name="Bedroom")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        assert speaker.name == "Bedroom"

    def test_initial_component_slots_are_none(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        assert speaker._discovery is None
        assert speaker._ws_manager is None
        assert speaker._metadata_service is None
        assert speaker._queue is None
        assert speaker._player is None
        assert speaker._backend is None
        assert speaker._proxy_server is None
        assert speaker._state_reporter is None
        assert speaker._queue_handler is None
        assert speaker._playback_handler is None
        assert speaker._volume_handler is None

    def test_build_component_config_maps_name_and_uuid(self):
        config = _make_speaker_config(name="Salon", uuid="abc-123")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.device.name == "Salon"
        assert comp.device.uuid == "abc-123"

    def test_build_component_config_maps_backend_dlna(self):
        config = _make_speaker_config(
            backend_type="dlna",
            dlna_ip="10.0.0.1",
            dlna_port=1400,
            dlna_fixed_volume=True,
            proxy_port=7120,
        )
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.backend.type == "dlna"
        assert comp.backend.dlna.ip == "10.0.0.1"
        assert comp.backend.dlna.port == 1400
        assert comp.backend.dlna.fixed_volume is True
        assert comp.backend.dlna.proxy_port == 7120

    def test_build_component_config_maps_backend_local(self):
        config = _make_speaker_config(
            backend_type="local",
            audio_device="hw:1",
            audio_buffer_size=4096,
        )
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.backend.type == "local"
        assert comp.backend.local.device == "hw:1"
        assert comp.backend.local.buffer_size == 4096

    def test_build_component_config_maps_server(self):
        config = _make_speaker_config(http_port=9000, bind_address="192.168.1.5")
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.server.http_port == 9000
        assert comp.server.bind_address == "192.168.1.5"

    def test_build_component_config_maps_quality(self):
        config = _make_speaker_config(max_quality=6)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert comp.qobuz.max_quality == 6

    def test_build_component_config_returns_config_instance(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")

        comp = speaker._build_component_config()

        assert isinstance(comp, Config)


class TestSpeakerLifecycle:
    async def test_start_creates_backend_and_discovery(self):
        """start() should set up backend, player, queue, and discovery."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock()
        mock_backend.name = "Mock DLNA Backend"
        # Not a DLNABackend instance, so proxy + fixed_volume branch is skipped
        mock_backend.__class__ = object  # not DLNABackend

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc = mock_disc_cls.return_value
            mock_disc.start = AsyncMock()

            result = await speaker.start()

        assert result is True
        assert speaker._is_running is True
        assert speaker._backend is mock_backend
        assert speaker._player is not None
        assert speaker._queue is not None
        assert speaker._discovery is not None

    async def test_start_with_dlna_backend_creates_proxy(self):
        """For a real DLNABackend, start() should create AudioProxyServer."""
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = 7

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch(
                "qobuz_proxy.speaker.AudioProxyServer",
                return_value=mock_proxy,
            ),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()

            result = await speaker.start()

        assert result is True
        assert speaker._proxy_server is mock_proxy
        mock_proxy.start.assert_called_once()
        mock_backend.set_proxy_server.assert_called_once_with(mock_proxy)

    async def test_start_auto_quality_dlna_uses_recommended(self):
        """AUTO_QUALITY with DLNA should resolve to get_recommended_quality()."""
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = 6  # CD quality

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.AudioProxyServer", return_value=mock_proxy),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()
            await speaker.start()

        assert speaker._effective_quality == 6

    async def test_start_auto_quality_dlna_fallback(self):
        """AUTO_QUALITY with DLNA and no recommendation should use AUTO_FALLBACK_QUALITY."""
        from qobuz_proxy.backends.dlna import DLNABackend
        from qobuz_proxy.config import AUTO_FALLBACK_QUALITY

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = None  # No recommendation

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.AudioProxyServer", return_value=mock_proxy),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()
            await speaker.start()

        assert speaker._effective_quality == AUTO_FALLBACK_QUALITY

    async def test_start_failure_returns_false(self):
        """If BackendFactory raises, start() should return False and not raise."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with patch(
            "qobuz_proxy.speaker.BackendFactory.create_from_config",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ):
            result = await speaker.start()

        assert result is False
        assert speaker._is_running is False

    async def test_start_failure_does_not_raise(self):
        """start() should swallow exceptions and return False, not propagate."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        with patch(
            "qobuz_proxy.speaker.BackendFactory.create_from_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Must NOT raise
            result = await speaker.start()

        assert result is False

    async def test_stop_tears_down_components(self):
        """stop() should call stop/disconnect on all wired components."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        # Simulate already-started state
        speaker._is_running = True
        speaker._state_reporter = MagicMock()
        speaker._state_reporter.stop = AsyncMock()
        speaker._player = MagicMock()
        speaker._player.stop = AsyncMock()
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.stop = AsyncMock()
        speaker._discovery = MagicMock()
        speaker._discovery.stop = AsyncMock()
        speaker._proxy_server = MagicMock()
        speaker._proxy_server.stop = AsyncMock()
        speaker._backend = MagicMock()
        speaker._backend.disconnect = AsyncMock()

        await speaker.stop()

        speaker._state_reporter.stop.assert_called_once()
        speaker._player.stop.assert_called_once()
        speaker._ws_manager.stop.assert_called_once()
        speaker._discovery.stop.assert_called_once()
        speaker._proxy_server.stop.assert_called_once()
        speaker._backend.disconnect.assert_called_once()
        assert speaker._is_running is False

    async def test_stop_is_safe_with_no_components(self):
        """stop() on a speaker that never fully started should not raise."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        # All slots are None, _is_running is False
        await speaker.stop()  # Should not raise

    async def test_stop_continues_if_one_component_errors(self):
        """stop() should teardown all components even if some raise exceptions."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        speaker._is_running = True
        speaker._state_reporter = MagicMock()
        speaker._state_reporter.stop = AsyncMock(side_effect=Exception("state reporter error"))
        speaker._backend = MagicMock()
        speaker._backend.disconnect = AsyncMock()

        await speaker.stop()

        # backend.disconnect should still be called despite state_reporter failing
        speaker._backend.disconnect.assert_called_once()


class TestSpeakerWebSocket:
    async def test_setup_websocket_wires_gapless_rearm_callback(self):
        """Queue edits mid-track must re-arm gapless via the command handler callback."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        speaker._queue = MagicMock()
        player = MagicMock()
        player.start = AsyncMock()
        player.get_volume = AsyncMock(return_value=50)
        player.on_next_track_info_changed = AsyncMock()
        speaker._player = player

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        with (
            patch("qobuz_proxy.speaker.WsManager") as mock_ws_cls,
            patch("qobuz_proxy.speaker.StateReporter") as mock_sr_cls,
        ):
            mock_ws = mock_ws_cls.return_value
            mock_ws.start = AsyncMock()
            mock_ws.send_volume_changed = AsyncMock()
            mock_sr_cls.return_value.start = AsyncMock()

            await speaker._setup_websocket(tokens)

        assert speaker._playback_handler is not None
        assert speaker._playback_handler._on_next_track_changed is player.on_next_track_info_changed

    async def test_setup_websocket_refreshes_existing_manager(self):
        """If ws_manager already exists, _setup_websocket should only refresh tokens."""
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")
        speaker._queue = MagicMock()
        speaker._player = MagicMock()
        speaker._ws_manager = MagicMock()

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="new_jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        with patch("qobuz_proxy.speaker.WsManager") as mock_ws_cls:
            await speaker._setup_websocket(tokens)

        # Should refresh tokens on existing manager, not create a new one
        speaker._ws_manager.set_tokens.assert_called_once_with(tokens)
        mock_ws_cls.assert_not_called()
        assert speaker._ws_connected_event.is_set() is True

    async def test_on_app_connected_creates_task(self):
        """_on_app_connected should schedule _setup_websocket as an asyncio task."""
        import asyncio

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="jwt",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        setup_calls = []

        async def fake_setup(t):
            setup_calls.append(t)

        speaker._setup_websocket = fake_setup  # type: ignore[method-assign]
        speaker._on_app_connected(tokens)

        # Let the event loop process the created task
        await asyncio.sleep(0)

        assert len(setup_calls) == 1
        assert setup_calls[0] is tokens


class TestSpeakerQualityChange:
    async def test_quality_change_updates_effective_quality(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._metadata_service = MagicMock()
        speaker._player = MagicMock()
        speaker._player.reload_current_track = AsyncMock()

        await speaker._on_quality_change(6)

        assert speaker._effective_quality == 6
        speaker._metadata_service.set_max_quality.assert_called_once_with(6)
        speaker._player.reload_current_track.assert_called_once()

    async def test_quality_change_noop_if_same(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._effective_quality = 27
        speaker._metadata_service = MagicMock()
        speaker._player = MagicMock()
        speaker._player.reload_current_track = AsyncMock()

        await speaker._on_quality_change(27)

        speaker._metadata_service.set_max_quality.assert_not_called()
        speaker._player.reload_current_track.assert_not_called()


class TestSpeakerStateReport:
    async def test_send_state_report_maps_loading_to_stopped(self):
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.send_state_update = AsyncMock()

        report = MagicMock()
        report.playing_state = PlaybackState.LOADING
        report.buffer_state = 1
        report.position_value_ms = 0
        report.duration_ms = 60000
        report.current_queue_item_id = "item-1"
        report.queue_version_major = 1
        report.queue_version_minor = 0

        await speaker._send_state_report(report)

        call_kwargs = speaker._ws_manager.send_state_update.call_args.kwargs
        assert call_kwargs["playing_state"] == int(PlaybackState.STOPPED)

    async def test_send_state_report_maps_error_to_stopped(self):
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = MagicMock()
        speaker._ws_manager.send_state_update = AsyncMock()

        report = MagicMock()
        report.playing_state = PlaybackState.ERROR
        report.buffer_state = 1
        report.position_value_ms = 0
        report.duration_ms = 0
        report.current_queue_item_id = ""
        report.queue_version_major = 0
        report.queue_version_minor = 0

        await speaker._send_state_report(report)

        call_kwargs = speaker._ws_manager.send_state_update.call_args.kwargs
        assert call_kwargs["playing_state"] == int(PlaybackState.STOPPED)

    async def test_send_state_report_noop_without_ws_manager(self):
        config = _make_speaker_config()
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._ws_manager = None

        report = MagicMock()
        # Should not raise
        await speaker._send_state_report(report)


class TestNowPlayingStatus:
    """get_status() must not report a volume the proxy doesn't control."""

    @staticmethod
    def _playing_speaker(**config_kwargs) -> Speaker:
        from qobuz_proxy.backends import PlaybackState

        config = _make_speaker_config(**config_kwargs)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        speaker._is_running = True

        player = MagicMock()
        player.state = PlaybackState.PLAYING
        player.current_track.metadata = {"title": "Song", "quality_name": "FLAC Hi-Res"}
        player._volume = 50  # Initial default — never updated in fixed-volume mode
        speaker._player = player
        return speaker

    def test_reports_volume_when_proxy_controls_it(self):
        speaker = self._playing_speaker(dlna_fixed_volume=False)
        np = speaker.get_status()["now_playing"]

        assert np["title"] == "Song"
        assert np["volume"] == 50

    def test_omits_volume_for_fixed_volume_speaker(self):
        speaker = self._playing_speaker(dlna_fixed_volume=True)
        status = speaker.get_status()

        assert "volume" not in status["now_playing"]
        assert status["config"]["fixed_volume"] is True

    def test_local_speaker_ignores_stale_fixed_volume_flag(self):
        """Fixed volume is a DLNA-only setting; a leftover flag on a local speaker
        must not hide the volume the proxy does control."""
        speaker = self._playing_speaker(backend_type="local", dlna_fixed_volume=True)
        np = speaker.get_status()["now_playing"]

        assert np["volume"] == 50


class TestQualitySourceStatus:
    """get_status() must say where the effective quality came from."""

    @staticmethod
    async def _start_auto_speaker(recommended, confirmed):
        from qobuz_proxy.backends.dlna import DLNABackend

        config = _make_speaker_config(max_quality=AUTO_QUALITY)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="app-id")

        mock_backend = MagicMock(spec=DLNABackend)
        mock_backend.name = "Mock DLNA"
        mock_backend.get_recommended_quality.return_value = recommended
        mock_backend.quality_detection_confirmed = confirmed

        mock_proxy = MagicMock()
        mock_proxy.start = AsyncMock()

        with (
            patch(
                "qobuz_proxy.speaker.BackendFactory.create_from_config",
                new_callable=AsyncMock,
                return_value=mock_backend,
            ),
            patch("qobuz_proxy.speaker.AudioProxyServer", return_value=mock_proxy),
            patch("qobuz_proxy.speaker.DiscoveryService") as mock_disc_cls,
        ):
            mock_disc_cls.return_value.start = AsyncMock()
            await speaker.start()
        return speaker

    async def test_confirmed_auto_detection(self):
        speaker = await self._start_auto_speaker(recommended=7, confirmed=True)
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == "auto"
        assert cfg["effective_quality"] == 7
        assert cfg["quality_source"] == "auto"

    async def test_unconfirmed_auto_detection_is_fallback(self):
        """gmediarender-style device: quality is a guess, the UI must say so."""
        speaker = await self._start_auto_speaker(recommended=6, confirmed=False)
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == "auto"
        assert cfg["effective_quality"] == 6
        assert cfg["quality_source"] == "auto_fallback"

    async def test_no_recommendation_is_fallback(self):
        speaker = await self._start_auto_speaker(recommended=None, confirmed=False)
        cfg = speaker.get_status()["config"]

        assert cfg["quality_source"] == "auto_fallback"

    def test_manual_quality(self):
        config = _make_speaker_config(max_quality=27)
        speaker = Speaker(config=config, api_client=_make_api_client(), app_id="id")
        cfg = speaker.get_status()["config"]

        assert cfg["max_quality"] == 27
        assert cfg["effective_quality"] == 27
        assert cfg["quality_source"] == "manual"
