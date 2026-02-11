"""Tests for LocalAudioBackend playback (QPROXY-021)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import numpy as np
import pytest

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_AUDIO_44100 = np.random.rand(44100, 2).astype(np.float32)  # 1 second
FAKE_AUDIO_96000 = np.random.rand(96000, 2).astype(np.float32)  # 1 second


def _mock_sounddevice():
    """Create a mock sounddevice module."""
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
_SF_PATCH = "qobuz_proxy.backends.local.backend.soundfile"


def _make_metadata() -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id="123",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration_ms=1000,
    )


def _mock_aiohttp_session(data: bytes = b"fake-flac-data"):
    """Create a mock aiohttp.ClientSession context manager."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.read = AsyncMock(return_value=data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


async def _create_connected_backend() -> LocalAudioBackend:
    """Create a backend that's connected with mocked device."""
    backend = LocalAudioBackend(device="default", buffer_size=2048)
    with patch(_SD_PATCH, return_value=_mock_sounddevice()):
        await backend.connect()
    return backend


# ---------------------------------------------------------------------------
# Tests: State Transitions
# ---------------------------------------------------------------------------


class TestPlayStateTransitions:
    """Test state transitions during play()."""

    async def test_play_transitions_loading_then_playing(self) -> None:
        backend = await _create_connected_backend()
        states: list[PlaybackState] = []
        backend.on_state_change(lambda s: states.append(s))

        mock_sf = MagicMock()
        mock_sf.read.return_value = (FAKE_AUDIO_44100.copy(), 44100)

        with (
            patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session()),
            patch("qobuz_proxy.backends.local.backend.sf", mock_sf, create=True),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):
            async def patched_download(url):
                return FAKE_AUDIO_44100.copy(), 44100

            backend._download_and_decode = patched_download

            # Mock stream methods
            backend._stream.set_ring_buffer = MagicMock()
            backend._stream.open = MagicMock()
            backend._stream.start = MagicMock()

            await backend.play("http://example.com/track.flac", _make_metadata())

            # Let the feeding task start
            await asyncio.sleep(0.01)

        assert PlaybackState.LOADING in states
        assert PlaybackState.PLAYING in states
        assert states.index(PlaybackState.LOADING) < states.index(PlaybackState.PLAYING)

        await backend.stop()
        await backend.disconnect()

    async def test_play_download_failure(self) -> None:
        backend = await _create_connected_backend()
        states: list[PlaybackState] = []
        errors: list[str] = []
        backend.on_state_change(lambda s: states.append(s))
        backend.on_playback_error(lambda e: errors.append(e))

        async def failing_download(url):
            raise aiohttp.ClientError("Download failed")

        backend._download_and_decode = failing_download

        await backend.play("http://example.com/track.flac", _make_metadata())

        assert PlaybackState.LOADING in states
        assert PlaybackState.ERROR in states
        assert len(errors) == 1
        assert "Download failed" in errors[0]

        await backend.disconnect()

    async def test_play_decode_failure(self) -> None:
        backend = await _create_connected_backend()
        errors: list[str] = []
        backend.on_playback_error(lambda e: errors.append(e))

        async def failing_decode(url):
            raise RuntimeError("Decode error: unsupported format")

        backend._download_and_decode = failing_decode

        await backend.play("http://example.com/track.flac", _make_metadata())

        assert backend._state == PlaybackState.ERROR
        assert len(errors) == 1
        assert "Decode error" in errors[0]

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Pause / Resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    async def test_pause_resume(self) -> None:
        backend = await _create_connected_backend()

        # Start playback with patched download
        async def fake_download(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.pause = MagicMock()
        backend._stream.resume = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        await backend.pause()
        assert backend._state == PlaybackState.PAUSED
        backend._stream.pause.assert_called_once()

        await backend.resume()
        assert backend._state == PlaybackState.PLAYING
        backend._stream.resume.assert_called_once()

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_during_playback(self) -> None:
        backend = await _create_connected_backend()

        async def fake_download(url):
            # Return a lot of audio so feeding loop doesn't finish instantly
            return np.random.rand(441000, 2).astype(np.float32), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        assert backend._feeding_task is not None
        assert not backend._feeding_task.done()

        await backend.stop()

        assert backend._state == PlaybackState.STOPPED
        assert backend._feeding_task is None
        assert backend._audio_data is None
        assert backend._frames_fed == 0
        backend._stream.stop.assert_called_once()

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Track End
# ---------------------------------------------------------------------------


class TestTrackEnd:
    async def test_track_ends_naturally(self) -> None:
        backend = await _create_connected_backend()
        ended = []
        backend.on_track_ended(lambda: ended.append(True))

        # Small audio so it finishes quickly
        small_audio = np.random.rand(100, 2).astype(np.float32)

        async def fake_download(url):
            return small_audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())

        # The ring buffer has 10s * 44100 = 441000 frame capacity
        # With 100 frames, feeding finishes instantly. But draining waits
        # for ring_buffer.available() == 0. Since nothing reads from
        # the buffer in tests, we need to manually drain it.
        await asyncio.sleep(0.05)

        # The feeding loop fed all 100 frames. Now manually drain the buffer
        # so the drain check passes.
        if backend._ring_buffer:
            backend._ring_buffer.read(backend._ring_buffer.available())

        # Wait for drain check
        await asyncio.sleep(0.2)

        assert len(ended) == 1
        assert backend._state == PlaybackState.STOPPED

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Sample Rate Change
# ---------------------------------------------------------------------------


class TestSampleRateChange:
    async def test_stream_reopened_on_sample_rate_change(self) -> None:
        backend = await _create_connected_backend()
        open_calls = []

        def track_open(sample_rate, channels=2):
            open_calls.append((sample_rate, channels))

        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = track_open
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()

        # First track at 44100
        async def fake_download_44100(url):
            return FAKE_AUDIO_44100.copy(), 44100

        backend._download_and_decode = fake_download_44100
        await backend.play("http://example.com/track1.flac", _make_metadata())
        await asyncio.sleep(0.01)
        await backend.stop()

        # Second track at 96000
        async def fake_download_96000(url):
            return FAKE_AUDIO_96000.copy(), 96000

        backend._download_and_decode = fake_download_96000
        await backend.play("http://example.com/track2.flac", _make_metadata())
        await asyncio.sleep(0.01)

        assert len(open_calls) == 2
        assert open_calls[0][0] == 44100
        assert open_calls[1][0] == 96000

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Volume
# ---------------------------------------------------------------------------


class TestVolume:
    async def test_volume_delegated_to_stream(self) -> None:
        backend = await _create_connected_backend()
        backend._stream.set_volume = MagicMock()

        await backend.set_volume(75)
        assert await backend.get_volume() == 75
        backend._stream.set_volume.assert_called_with(75)

        await backend.disconnect()

    async def test_volume_clamped(self) -> None:
        backend = await _create_connected_backend()
        backend._stream.set_volume = MagicMock()

        await backend.set_volume(150)
        assert await backend.get_volume() == 100

        await backend.set_volume(-10)
        assert await backend.get_volume() == 0

        await backend.disconnect()

    async def test_volume_before_connect(self) -> None:
        backend = LocalAudioBackend()
        await backend.set_volume(80)
        assert await backend.get_volume() == 80


# ---------------------------------------------------------------------------
# Tests: Seek
# ---------------------------------------------------------------------------


class TestSeek:
    async def test_seek_sets_target(self) -> None:
        backend = await _create_connected_backend()
        backend._sample_rate = 44100

        await backend.seek(5000)  # 5 seconds
        assert backend._seek_target == int(5000 / 1000 * 44100)

        await backend.disconnect()

    async def test_seek_no_op_without_sample_rate(self) -> None:
        backend = await _create_connected_backend()
        assert backend._sample_rate == 0

        await backend.seek(5000)
        assert backend._seek_target is None

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Position
# ---------------------------------------------------------------------------


class TestPosition:
    async def test_position_zero_initially(self) -> None:
        backend = await _create_connected_backend()
        assert await backend.get_position() == 0
        await backend.disconnect()

    async def test_position_reflects_frames_fed(self) -> None:
        backend = await _create_connected_backend()
        backend._sample_rate = 44100
        backend._frames_fed = 44100  # 1 second

        pos = await backend.get_position()
        assert pos == 1000  # 1000ms

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Connect / Disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_creates_stream(self) -> None:
        backend = LocalAudioBackend()
        with patch(_SD_PATCH, return_value=_mock_sounddevice()):
            result = await backend.connect()

        assert result is True
        assert backend.is_connected()
        assert backend._stream is not None
        assert "Test Output" in backend.name

        await backend.disconnect()

    async def test_connect_failure(self) -> None:
        sd = _mock_sounddevice()
        sd.query_devices.return_value = []  # No devices

        backend = LocalAudioBackend()
        with patch(_SD_PATCH, return_value=sd):
            result = await backend.connect()

        assert result is False
        assert not backend.is_connected()

    async def test_disconnect_stops_playback(self) -> None:
        backend = await _create_connected_backend()

        async def fake_download(url):
            return np.random.rand(441000, 2).astype(np.float32), 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()
        backend._stream.stop = MagicMock()
        backend._stream.close = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.01)

        await backend.disconnect()

        assert not backend.is_connected()
        assert backend._stream is None

    async def test_get_info(self) -> None:
        backend = LocalAudioBackend(device="usb-dac")
        info = backend.get_info()
        assert info.backend_type == "local"
        assert info.device_id == "local-usb-dac"


# ---------------------------------------------------------------------------
# Tests: Download and Decode
# ---------------------------------------------------------------------------


class TestDownloadAndDecode:
    async def test_download_and_decode_stereo(self) -> None:
        backend = LocalAudioBackend()

        mock_sf = MagicMock()
        mock_sf.read.return_value = (FAKE_AUDIO_44100.copy(), 44100)

        mock_session = _mock_aiohttp_session()

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):
            audio, sr = await backend._download_and_decode("http://example.com/track.flac")

        assert sr == 44100
        assert audio.shape == (44100, 2)
        assert audio.dtype == np.float32

    async def test_download_and_decode_mono(self) -> None:
        backend = LocalAudioBackend()
        mono_data = np.random.rand(44100).astype(np.float32)  # 1D

        mock_sf = MagicMock()
        mock_sf.read.return_value = (mono_data, 44100)

        mock_session = _mock_aiohttp_session()

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": mock_sf}),
        ):
            audio, sr = await backend._download_and_decode("http://example.com/track.flac")

        assert audio.shape == (44100, 1)  # Reshaped to 2D

    async def test_download_http_error(self) -> None:
        backend = LocalAudioBackend()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=404,
                message="Not Found",
            )
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict("sys.modules", {"soundfile": MagicMock()}),
        ):
            with pytest.raises(aiohttp.ClientResponseError):
                await backend._download_and_decode("http://example.com/track.flac")


# ---------------------------------------------------------------------------
# Tests: Feeding Loop
# ---------------------------------------------------------------------------


class TestFeedingLoop:
    async def test_feeding_loop_feeds_all_frames(self) -> None:
        backend = await _create_connected_backend()

        audio = np.random.rand(1000, 2).astype(np.float32)

        async def fake_download(url):
            return audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        positions: list[int] = []
        backend.on_position_update(lambda p: positions.append(p))

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.1)

        # All frames should be fed (ring buffer is large enough)
        assert backend._frames_fed == 1000
        assert len(positions) > 0

        # Position should reflect frames fed
        expected_ms = int(1000 / 44100 * 1000)
        assert positions[-1] == expected_ms

        await backend.stop()
        await backend.disconnect()

    async def test_feeding_loop_seek(self) -> None:
        backend = await _create_connected_backend()

        # 10 seconds of audio at 44100
        audio = np.random.rand(441000, 2).astype(np.float32)

        async def fake_download(url):
            return audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.05)

        # Seek to 5 seconds
        await backend.seek(5000)
        await asyncio.sleep(0.1)

        # frames_fed should be at or past 5s mark
        assert backend._frames_fed >= int(5000 / 1000 * 44100)

        await backend.stop()
        await backend.disconnect()
