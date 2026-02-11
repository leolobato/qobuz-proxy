"""Tests for seek and position tracking (QPROXY-022)."""

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _make_metadata() -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id="123",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration_ms=10000,
    )


async def _create_connected_backend() -> LocalAudioBackend:
    backend = LocalAudioBackend(device="default", buffer_size=2048)
    with patch(_SD_PATCH, return_value=_mock_sounddevice()):
        await backend.connect()
    return backend


async def _start_playback(
    backend: LocalAudioBackend,
    total_frames: int = 441000,
    sample_rate: int = 44100,
) -> None:
    """Start playback with fake audio data."""
    audio = np.random.rand(total_frames, 2).astype(np.float32)

    async def fake_download(url):
        return audio, sample_rate

    backend._download_and_decode = fake_download
    backend._stream.set_ring_buffer = MagicMock()
    backend._stream.open = MagicMock()
    backend._stream.start = MagicMock()
    backend._stream.stop = MagicMock()
    backend._stream.pause = MagicMock()
    backend._stream.resume = MagicMock()

    await backend.play("http://example.com/track.flac", _make_metadata())
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Tests: Position with buffer latency correction
# ---------------------------------------------------------------------------


class TestPositionBufferLatency:
    async def test_position_with_buffer_latency(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Manually set known state for deterministic test
        backend._frames_fed = 441000  # 10 seconds fed
        # Ring buffer has some frames in it (not yet played)
        buffer_available = backend._ring_buffer.available()

        pos = await backend.get_position()
        expected_raw_ms = 441000 / 44100 * 1000  # 10000ms
        expected_latency_ms = buffer_available / 44100 * 1000
        expected = int(expected_raw_ms - expected_latency_ms)

        assert pos == expected

        await backend.stop()
        await backend.disconnect()

    async def test_position_zero_when_stopped(self) -> None:
        backend = await _create_connected_backend()
        assert await backend.get_position() == 0
        await backend.disconnect()

    async def test_position_never_negative(self) -> None:
        """Position should never go below 0 even with large buffer latency."""
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Set frames_fed to very small value (less than what's in the buffer)
        backend._frames_fed = 10

        pos = await backend.get_position()
        assert pos >= 0

        await backend.stop()
        await backend.disconnect()

    async def test_position_no_buffer(self) -> None:
        """Position works when ring buffer is None (before play)."""
        backend = await _create_connected_backend()
        backend._sample_rate = 44100
        backend._frames_fed = 44100
        backend._ring_buffer = None

        pos = await backend.get_position()
        assert pos == 1000  # 1 second, no buffer correction

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Seek forward and backward
# ---------------------------------------------------------------------------


class TestSeekBasic:
    async def test_seek_forward(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Seek to 5 seconds
        await backend.seek(5000)
        await asyncio.sleep(0.1)

        # frames_fed should be at or past 5s mark
        expected_frame = int(5000 / 1000 * 44100)
        assert backend._frames_fed >= expected_frame

        await backend.stop()
        await backend.disconnect()

    async def test_seek_backward(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Let it feed for a bit
        await asyncio.sleep(0.1)

        # Seek backward to 1 second
        await backend.seek(1000)
        await asyncio.sleep(0.1)

        # After seek, frames_fed should reflect position near 1s
        expected_frame = int(1000 / 1000 * 44100)
        # It may have fed more since the seek, but it started from ~44100
        assert backend._frames_fed >= expected_frame

        await backend.stop()
        await backend.disconnect()

    async def test_seek_to_zero(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        await asyncio.sleep(0.05)
        await backend.seek(0)
        await asyncio.sleep(0.1)

        # Should have restarted from beginning, so position should be small
        pos = await backend.get_position()
        # It may have fed a chunk by now, but position should be reasonable
        assert pos < 5000  # Less than 5 seconds

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Seek edge cases
# ---------------------------------------------------------------------------


class TestSeekEdgeCases:
    async def test_seek_beyond_duration(self) -> None:
        backend = await _create_connected_backend()
        ended = []
        backend.on_track_ended(lambda: ended.append(True))

        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Seek beyond 10 seconds
        await backend.seek(15000)
        await asyncio.sleep(0.05)

        assert len(ended) == 1
        assert backend._state == PlaybackState.STOPPED
        assert backend._frames_fed == 441000

        await backend.disconnect()

    async def test_seek_negative_clamped(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        # Seek to negative (should clamp to 0)
        await backend.seek(-5000)
        await asyncio.sleep(0.1)

        # Position should be near beginning
        pos = await backend.get_position()
        assert pos < 5000

        await backend.stop()
        await backend.disconnect()

    async def test_seek_no_op_without_audio(self) -> None:
        backend = await _create_connected_backend()
        # No audio loaded
        await backend.seek(5000)
        assert backend._seek_target is None

        await backend.disconnect()

    async def test_seek_no_op_without_sample_rate(self) -> None:
        backend = await _create_connected_backend()
        assert backend._sample_rate == 0
        await backend.seek(5000)
        assert backend._seek_target is None

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Seek while paused
# ---------------------------------------------------------------------------


class TestSeekWhilePaused:
    async def test_seek_while_paused_stays_paused(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        await backend.pause()
        assert backend._state == PlaybackState.PAUSED

        await backend.seek(5000)
        await asyncio.sleep(0.1)

        # State should remain PAUSED
        assert backend._state == PlaybackState.PAUSED

        await backend.stop()
        await backend.disconnect()

    async def test_seek_while_paused_updates_position(self) -> None:
        backend = await _create_connected_backend()
        await _start_playback(backend, total_frames=441000, sample_rate=44100)

        await backend.pause()
        await backend.seek(5000)
        await asyncio.sleep(0.1)

        # Position should reflect the seek target (approx 5 seconds)
        pos = await backend.get_position()
        # Allow some tolerance since feeding may have advanced a bit
        assert pos >= 4000

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Position updates during feeding
# ---------------------------------------------------------------------------


class TestPositionUpdates:
    async def test_position_callbacks_called(self) -> None:
        backend = await _create_connected_backend()
        positions: list[int] = []
        backend.on_position_update(lambda p: positions.append(p))

        await _start_playback(backend, total_frames=44100, sample_rate=44100)
        await asyncio.sleep(0.1)

        assert len(positions) > 0

        await backend.stop()
        await backend.disconnect()

    async def test_position_updates_are_buffer_corrected(self) -> None:
        """Position updates should account for buffer latency."""
        backend = await _create_connected_backend()
        positions: list[int] = []
        backend.on_position_update(lambda p: positions.append(p))

        # Use a small amount of audio so feeding finishes quickly
        await _start_playback(backend, total_frames=44100, sample_rate=44100)
        await asyncio.sleep(0.1)

        # All 44100 frames (1s) should be fed. The reported positions
        # should be <= 1000ms (raw) and potentially less due to buffer correction
        for pos in positions:
            assert pos <= 1000

        await backend.stop()
        await backend.disconnect()

    async def test_seek_triggers_immediate_position_update(self) -> None:
        """Seek should notify position immediately, not wait for next chunk."""
        backend = await _create_connected_backend()
        positions: list[int] = []
        backend.on_position_update(lambda p: positions.append(p))

        await _start_playback(backend, total_frames=441000, sample_rate=44100)
        await asyncio.sleep(0.05)

        positions.clear()
        await backend.seek(5000)
        await asyncio.sleep(0.05)

        # Should have at least one position update near 5000ms
        assert len(positions) > 0
        # The first update after seek should be near 5 seconds
        assert any(4000 <= p <= 6000 for p in positions)

        await backend.stop()
        await backend.disconnect()
