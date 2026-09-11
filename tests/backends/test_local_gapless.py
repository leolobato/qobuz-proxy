"""Tests for LocalAudioBackend gapless playback."""

import asyncio
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import numpy as np
import pytest

import qobuz_proxy.backends.local.backend as local_backend_module
from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TRACK1_FRAMES = 1000
TRACK2_FRAMES = 2000

AUDIO_TRACK1 = np.random.rand(TRACK1_FRAMES, 2).astype(np.float32)
AUDIO_TRACK2 = np.random.rand(TRACK2_FRAMES, 2).astype(np.float32)

_SD_PATCH = "qobuz_proxy.backends.local.device._import_sounddevice"


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


def _make_metadata(track_id: str = "123") -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id=track_id,
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration_ms=1000,
    )


async def _create_playing_backend(
    audio: np.ndarray = AUDIO_TRACK1, sample_rate: int = 44100
) -> LocalAudioBackend:
    """Create a connected backend already playing `audio` with mocked stream."""
    backend = LocalAudioBackend(device="default", buffer_size=2048)
    with patch(_SD_PATCH, return_value=_mock_sounddevice()):
        await backend.connect()

    async def fake_download_and_decode(url):
        return audio.copy(), sample_rate

    backend._download_and_decode = fake_download_and_decode
    backend._stream.set_ring_buffer = MagicMock()
    backend._stream.open = MagicMock()
    backend._stream.start = MagicMock()
    backend._stream.stop = MagicMock()
    backend._stream.pause = MagicMock()

    await backend.play("http://example.com/track1.flac", _make_metadata("1"))
    return backend


async def _wait_until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll until cond() is true or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("Timed out waiting for condition")
        await asyncio.sleep(0.01)


def _arm_next_track(
    backend: LocalAudioBackend,
    audio: np.ndarray = AUDIO_TRACK2,
    sample_rate: int = 44100,
) -> None:
    """Patch prefetch internals so set_next_track resolves to `audio`."""
    backend._download = AsyncMock(return_value=b"fake-flac-bytes")
    backend._decode = AsyncMock(return_value=(audio.copy(), sample_rate))


async def _drain_after_feed(backend: LocalAudioBackend, frames: int) -> None:
    """Wait until the current track is fully fed, then consume the buffer.

    Nothing reads from the ring buffer in tests, so draining simulates the
    audio callback playing everything out. Reading before the feeding task has
    run would be a no-op and leave the drain loop waiting forever.
    """
    await _wait_until(lambda: backend._ring_buffer.available() >= frames)
    backend._ring_buffer.read(backend._ring_buffer.available())


# ---------------------------------------------------------------------------
# Tests: Contract
# ---------------------------------------------------------------------------


class TestGaplessContract:
    async def test_supports_gapless(self) -> None:
        backend = LocalAudioBackend()
        assert backend.supports_gapless is True

    async def test_set_next_track_starts_prefetch(self) -> None:
        backend = LocalAudioBackend()
        _arm_next_track(backend)

        result = await backend.set_next_track(
            "http://example.com/next.flac", _make_metadata("2"), queue_item_id=7
        )

        assert result is True
        assert backend._next_prefetch_task is not None
        await _wait_until(lambda: backend._next_prefetch_task.done())
        assert backend._next_prefetch_task.result() == b"fake-flac-bytes"

        await backend.clear_next_track()

    async def test_clear_next_track_cancels_prefetch(self) -> None:
        backend = LocalAudioBackend()

        async def slow_download(url):
            await asyncio.sleep(10)
            return b"never"

        backend._download = slow_download

        await backend.set_next_track("http://example.com/next.flac", _make_metadata("2"))
        task = backend._next_prefetch_task
        assert task is not None

        await backend.clear_next_track()

        assert backend._next_prefetch_task is None
        assert backend._next_track_meta is None
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Tests: Seamless transition (same format)
# ---------------------------------------------------------------------------


class TestSeamlessTransition:
    async def test_transition_swaps_audio_without_track_end(self) -> None:
        backend = await _create_playing_backend()
        ended: list[bool] = []
        started: list[bool] = []
        backend.on_track_ended(lambda: ended.append(True))
        backend.on_next_track_started(lambda: started.append(True))

        _arm_next_track(backend)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))

        # Track 1 feeds fully, then the loop swaps in track 2 and keeps feeding
        # the same ring buffer.
        await _wait_until(lambda: backend._transition_pending)
        assert backend._total_frames == TRACK2_FRAMES
        assert backend._ring_buffer.available() == TRACK1_FRAMES + TRACK2_FRAMES

        # Nothing consumed yet: old track's tail still buffered, no callbacks
        assert started == []
        assert ended == []

        # Consume past the old track's tail — boundary crossed
        backend._ring_buffer.read(TRACK1_FRAMES + 1)
        await _wait_until(lambda: len(started) == 1)
        assert not backend._transition_pending
        assert ended == []

        # Drain the rest: track 2 ends naturally (nothing else armed)
        backend._ring_buffer.read(backend._ring_buffer.available())
        await _wait_until(lambda: len(ended) == 1)
        assert len(started) == 1
        assert backend._state == PlaybackState.STOPPED

        await backend.disconnect()

    async def test_stream_not_reconfigured_on_same_format(self) -> None:
        backend = await _create_playing_backend()
        backend._stream.open.reset_mock()

        _arm_next_track(backend)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))

        await _wait_until(lambda: backend._transition_pending)

        # Same sample rate/channels: no stream reopen, same ring buffer
        backend._stream.open.assert_not_called()

        await backend.stop()
        await backend.disconnect()

    async def test_position_reports_old_track_tail_then_new_track(self) -> None:
        backend = await _create_playing_backend()

        _arm_next_track(backend)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        await _wait_until(lambda: backend._transition_pending)

        # Nothing consumed: old track has not advanced past what already played
        assert await backend.get_position() == 0

        # Consume half of the old track's tail: position is on the old track
        backend._ring_buffer.read(TRACK1_FRAMES // 2)
        expected_old_ms = int((TRACK1_FRAMES // 2) / 44100 * 1000)
        assert await backend.get_position() == expected_old_ms

        # Consume past the boundary: position restarts on the new track
        backend._ring_buffer.read(TRACK1_FRAMES - TRACK1_FRAMES // 2 + 100)
        new_pos = await backend.get_position()
        assert new_pos == int(100 / 44100 * 1000)

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Format change transition
# ---------------------------------------------------------------------------


class TestFormatChangeTransition:
    async def test_stream_reopened_at_new_rate(self) -> None:
        backend = await _create_playing_backend()
        started: list[bool] = []
        ended: list[bool] = []
        backend.on_next_track_started(lambda: started.append(True))
        backend.on_track_ended(lambda: ended.append(True))
        backend._stream.open.reset_mock()

        hires_audio = np.random.rand(TRACK2_FRAMES, 2).astype(np.float32)
        _arm_next_track(backend, audio=hires_audio, sample_rate=96000)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))

        # The transition waits for the old buffer to drain before reconfiguring
        await _wait_until(lambda: backend._decode.call_count == 1)
        old_buffer = backend._ring_buffer
        old_buffer.read(old_buffer.available())

        await _wait_until(lambda: len(started) == 1)
        backend._stream.open.assert_called_once_with(96000, 2)
        assert backend._sample_rate == 96000
        assert backend._ring_buffer is not old_buffer
        assert backend._total_frames == TRACK2_FRAMES
        assert not backend._transition_pending
        assert ended == []

        await backend.stop()
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Fallback paths
# ---------------------------------------------------------------------------


class TestGaplessFallback:
    async def test_prefetch_failure_falls_back_to_track_end(self) -> None:
        backend = await _create_playing_backend()
        started: list[bool] = []
        ended: list[bool] = []
        backend.on_next_track_started(lambda: started.append(True))
        backend.on_track_ended(lambda: ended.append(True))

        backend._download = AsyncMock(side_effect=aiohttp.ClientError("network down"))
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))

        # Failed prefetch: normal drain + track-ended path
        await _drain_after_feed(backend, TRACK1_FRAMES)
        await _wait_until(lambda: len(ended) == 1)
        assert started == []
        assert backend._state == PlaybackState.STOPPED

        await backend.disconnect()

    async def test_stalled_download_gives_up_after_grace(self, monkeypatch) -> None:
        monkeypatch.setattr(local_backend_module, "NEXT_TRACK_GRACE_SECONDS", 0.1)

        backend = await _create_playing_backend()
        ended: list[bool] = []
        backend.on_track_ended(lambda: ended.append(True))

        async def stalled_download(url):
            await asyncio.sleep(60)
            return b"never"

        backend._download = stalled_download
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))

        # Drain the buffer: download still pending, grace period expires
        await _drain_after_feed(backend, TRACK1_FRAMES)
        await _wait_until(lambda: len(ended) == 1)
        assert backend._next_prefetch_task is None

        await backend.disconnect()

    async def test_clear_next_track_mid_playback_falls_back(self) -> None:
        backend = await _create_playing_backend()
        started: list[bool] = []
        ended: list[bool] = []
        backend.on_next_track_started(lambda: started.append(True))
        backend.on_track_ended(lambda: ended.append(True))

        async def slow_download(url):
            await asyncio.sleep(10)
            return b"never"

        backend._download = slow_download
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        await backend.clear_next_track()

        await _drain_after_feed(backend, TRACK1_FRAMES)
        await _wait_until(lambda: len(ended) == 1)
        assert started == []

        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: State clearing
# ---------------------------------------------------------------------------


class TestGaplessStateClearing:
    async def test_play_clears_armed_next_track(self) -> None:
        backend = await _create_playing_backend()

        async def slow_download(url):
            await asyncio.sleep(10)
            return b"never"

        backend._download = slow_download
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        assert backend._next_prefetch_task is not None

        await backend.play("http://example.com/track3.flac", _make_metadata("3"))
        assert backend._next_prefetch_task is None
        assert backend._transition_pending is False

        await backend.stop()
        await backend.disconnect()

    async def test_play_reuses_matching_prefetch(self) -> None:
        """Manual skip onto the armed next track must not re-download it."""
        backend = await _create_playing_backend()
        # Stop the feeding loop so a natural gapless transition cannot consume
        # the prefetch before play() — skip cancels feeding first as well.
        await backend._cancel_feeding()
        download_and_decode = AsyncMock(side_effect=AssertionError("should reuse prefetch"))
        backend._download_and_decode = download_and_decode

        _arm_next_track(backend)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        await _wait_until(
            lambda: backend._next_prefetch_task is not None and backend._next_prefetch_task.done()
        )

        await backend.play("http://example.com/track2.flac", _make_metadata("2"))

        backend._download.assert_awaited_once()
        backend._decode.assert_awaited_once_with(b"fake-flac-bytes")
        download_and_decode.assert_not_awaited()
        assert backend._next_prefetch_task is None
        assert backend._total_frames == TRACK2_FRAMES
        assert backend._state == PlaybackState.PLAYING

        await backend.stop()
        await backend.disconnect()

    async def test_play_waits_for_in_flight_matching_prefetch(self) -> None:
        """Skip must await the existing download rather than cancel and restart it."""
        backend = await _create_playing_backend()
        started = asyncio.Event()

        async def slow_download(url):
            started.set()
            await asyncio.sleep(0.05)
            return b"fake-flac-bytes"

        backend._download = slow_download
        backend._decode = AsyncMock(return_value=(AUDIO_TRACK2.copy(), 44100))
        download_and_decode = AsyncMock(side_effect=AssertionError("should reuse prefetch"))
        backend._download_and_decode = download_and_decode

        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        await started.wait()

        await backend.play("http://example.com/track2.flac", _make_metadata("2"))

        download_and_decode.assert_not_awaited()
        backend._decode.assert_awaited_once_with(b"fake-flac-bytes")
        assert backend._total_frames == TRACK2_FRAMES

        await backend.stop()
        await backend.disconnect()

    async def test_stop_clears_gapless_state(self) -> None:
        backend = await _create_playing_backend()

        _arm_next_track(backend)
        await backend.set_next_track("http://example.com/track2.flac", _make_metadata("2"))
        await _wait_until(lambda: backend._transition_pending)

        await backend.stop()

        assert backend._next_prefetch_task is None
        assert backend._transition_pending is False
        assert backend._state == PlaybackState.STOPPED

        await backend.disconnect()
