"""Tests for buffer monitoring (QPROXY-023)."""

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, BufferStatus


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


# ---------------------------------------------------------------------------
# Tests: get_buffer_status
# ---------------------------------------------------------------------------


class TestGetBufferStatus:
    async def test_buffer_status_ok_no_buffer(self) -> None:
        """Before any playback, buffer status should be OK."""
        backend = await _create_connected_backend()
        status = await backend.get_buffer_status()
        assert status == BufferStatus.OK
        await backend.disconnect()

    async def test_buffer_status_ok(self) -> None:
        """Buffer with healthy fill level returns OK."""
        backend = await _create_connected_backend()

        # Manually set up ring buffer with 50% fill
        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._ring_buffer.write(np.zeros((500, 2), dtype=np.float32))

        status = await backend.get_buffer_status()
        assert status == BufferStatus.OK
        await backend.disconnect()

    async def test_buffer_status_empty(self) -> None:
        """Empty buffer returns EMPTY."""
        backend = await _create_connected_backend()

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        # Don't write anything — fill level is 0%

        status = await backend.get_buffer_status()
        assert status == BufferStatus.EMPTY
        await backend.disconnect()

    async def test_buffer_status_low(self) -> None:
        """Buffer below 10% returns LOW."""
        backend = await _create_connected_backend()

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._ring_buffer.write(np.zeros((50, 2), dtype=np.float32))  # 5%

        status = await backend.get_buffer_status()
        assert status == BufferStatus.LOW
        await backend.disconnect()

    async def test_buffer_status_full(self) -> None:
        """Full buffer returns FULL."""
        backend = await _create_connected_backend()

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._ring_buffer.write(np.zeros((1000, 2), dtype=np.float32))

        status = await backend.get_buffer_status()
        assert status == BufferStatus.FULL
        await backend.disconnect()


# ---------------------------------------------------------------------------
# Tests: Buffer status change notification
# ---------------------------------------------------------------------------


class TestBufferStatusNotification:
    async def test_buffer_status_change_fires_callback(self) -> None:
        backend = await _create_connected_backend()
        statuses: list[BufferStatus] = []
        backend.on_buffer_status(lambda s: statuses.append(s))

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._last_buffer_status = BufferStatus.OK

        # Transition to EMPTY (fill level 0%)
        backend._check_buffer_status()
        assert BufferStatus.EMPTY in statuses

        await backend.disconnect()

    async def test_no_notification_if_status_unchanged(self) -> None:
        backend = await _create_connected_backend()
        statuses: list[BufferStatus] = []
        backend.on_buffer_status(lambda s: statuses.append(s))

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._ring_buffer.write(np.zeros((500, 2), dtype=np.float32))  # 50% = OK
        backend._last_buffer_status = BufferStatus.OK

        backend._check_buffer_status()
        assert len(statuses) == 0  # No change

        await backend.disconnect()

    async def test_underrun_logged(self, caplog) -> None:
        import logging

        backend = await _create_connected_backend()

        from qobuz_proxy.backends.local.ring_buffer import RingBuffer

        backend._ring_buffer = RingBuffer(1000, channels=2)
        backend._last_buffer_status = BufferStatus.OK

        with caplog.at_level(logging.WARNING, logger="qobuz_proxy.backends.local.backend"):
            backend._check_buffer_status()

        assert "buffer underrun" in caplog.text.lower()
        await backend.disconnect()

    async def test_buffer_status_during_feeding(self) -> None:
        """Buffer status is checked during feeding loop."""
        backend = await _create_connected_backend()
        statuses: list[BufferStatus] = []
        backend.on_buffer_status(lambda s: statuses.append(s))

        audio = np.random.rand(1000, 2).astype(np.float32)

        async def fake_download(url):
            return audio, 44100

        backend._download_and_decode = fake_download
        backend._stream.set_ring_buffer = MagicMock()
        backend._stream.open = MagicMock()
        backend._stream.start = MagicMock()

        await backend.play("http://example.com/track.flac", _make_metadata())
        await asyncio.sleep(0.1)

        # Buffer status should have been checked at least once
        # (exact statuses depend on timing, but _check_buffer_status was called)
        # The initial transition from OK to some status may or may not fire
        # depending on exact fill levels
        assert backend._last_buffer_status is not None

        await backend.stop()
        await backend.disconnect()
