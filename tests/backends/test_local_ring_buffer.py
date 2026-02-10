"""Tests for the local audio ring buffer."""

import threading

import numpy as np
import pytest

from qobuz_proxy.backends.local.ring_buffer import RingBuffer


class TestRingBufferInit:
    """Test RingBuffer initialization."""

    def test_default_stereo(self) -> None:
        buf = RingBuffer(1024)
        assert buf.capacity == 1024
        assert buf.channels == 2
        assert buf.available() == 0
        assert buf.free_space() == 1024

    def test_mono(self) -> None:
        buf = RingBuffer(512, channels=1)
        assert buf.channels == 1
        assert buf.capacity == 512

    def test_fill_level_empty(self) -> None:
        buf = RingBuffer(1000)
        assert buf.fill_level() == pytest.approx(0.0)


class TestRingBufferWriteRead:
    """Test basic write and read operations."""

    def test_basic_write_read(self) -> None:
        buf = RingBuffer(1024, channels=2)
        data = np.random.rand(100, 2).astype(np.float32)

        written = buf.write(data)
        assert written == 100
        assert buf.available() == 100

        result = buf.read(100)
        assert result.shape == (100, 2)
        np.testing.assert_array_almost_equal(result, data)
        assert buf.available() == 0

    def test_mono_write_read(self) -> None:
        buf = RingBuffer(1024, channels=1)
        data = np.random.rand(50, 1).astype(np.float32)

        written = buf.write(data)
        assert written == 50

        result = buf.read(50)
        np.testing.assert_array_almost_equal(result, data)

    def test_multiple_writes_single_read(self) -> None:
        buf = RingBuffer(1024, channels=2)
        chunk1 = np.ones((30, 2), dtype=np.float32) * 0.5
        chunk2 = np.ones((20, 2), dtype=np.float32) * 0.8

        buf.write(chunk1)
        buf.write(chunk2)
        assert buf.available() == 50

        result = buf.read(50)
        np.testing.assert_array_almost_equal(result[:30], chunk1)
        np.testing.assert_array_almost_equal(result[30:], chunk2)

    def test_single_write_multiple_reads(self) -> None:
        buf = RingBuffer(1024, channels=2)
        data = np.random.rand(100, 2).astype(np.float32)
        buf.write(data)

        r1 = buf.read(40)
        r2 = buf.read(60)
        np.testing.assert_array_almost_equal(r1, data[:40])
        np.testing.assert_array_almost_equal(r2, data[40:])


class TestRingBufferCounters:
    """Test available, free_space, and fill_level."""

    def test_available_and_free_space(self) -> None:
        buf = RingBuffer(1000, channels=2)
        data = np.zeros((400, 2), dtype=np.float32)
        buf.write(data)

        assert buf.available() == 400
        assert buf.free_space() == 600

    def test_fill_level(self) -> None:
        buf = RingBuffer(1000, channels=2)
        data = np.zeros((500, 2), dtype=np.float32)
        buf.write(data)

        assert buf.fill_level() == pytest.approx(0.5)

    def test_fill_level_full(self) -> None:
        buf = RingBuffer(100, channels=2)
        data = np.zeros((100, 2), dtype=np.float32)
        buf.write(data)

        assert buf.fill_level() == pytest.approx(1.0)

    def test_counters_after_read(self) -> None:
        buf = RingBuffer(1000, channels=2)
        buf.write(np.zeros((600, 2), dtype=np.float32))
        buf.read(200)

        assert buf.available() == 400
        assert buf.free_space() == 600


class TestRingBufferWrapAround:
    """Test wrap-around behavior."""

    def test_write_wrap_around(self) -> None:
        buf = RingBuffer(100, channels=2)

        # Fill to 80%, then read 60% to advance the read pointer
        buf.write(np.zeros((80, 2), dtype=np.float32))
        buf.read(60)
        # write_pos=80, read_pos=60, available=20

        # Write 40 frames — should wrap from pos 80 to pos 20
        data = np.random.rand(40, 2).astype(np.float32)
        written = buf.write(data)
        assert written == 40
        assert buf.available() == 60

        # Read all — first the 20 zeros, then the 40 new frames
        result = buf.read(60)
        np.testing.assert_array_almost_equal(result[20:], data)

    def test_read_wrap_around(self) -> None:
        buf = RingBuffer(100, channels=2)

        # Advance positions: write 90, read 90
        buf.write(np.zeros((90, 2), dtype=np.float32))
        buf.read(90)
        # read_pos=90, write_pos=90

        # Write 30 frames — wraps from pos 90 to pos 20
        data = np.random.rand(30, 2).astype(np.float32)
        buf.write(data)

        # Read wraps from pos 90 to pos 20
        result = buf.read(30)
        np.testing.assert_array_almost_equal(result, data)


class TestRingBufferEdgeCases:
    """Test underrun, overflow, and clear."""

    def test_underrun_zero_padding(self) -> None:
        buf = RingBuffer(1024, channels=2)
        data = np.ones((50, 2), dtype=np.float32) * 0.7
        buf.write(data)

        result = buf.read(100)
        assert result.shape == (100, 2)
        # First 50 frames have data
        np.testing.assert_array_almost_equal(result[:50], data)
        # Last 50 frames are zero-padded
        np.testing.assert_array_equal(result[50:], np.zeros((50, 2), dtype=np.float32))

    def test_overflow_truncation(self) -> None:
        buf = RingBuffer(100, channels=2)
        buf.write(np.zeros((80, 2), dtype=np.float32))

        # Only 20 frames of free space
        big_data = np.ones((50, 2), dtype=np.float32)
        written = buf.write(big_data)
        assert written == 20
        assert buf.available() == 100

    def test_clear(self) -> None:
        buf = RingBuffer(1024, channels=2)
        buf.write(np.random.rand(500, 2).astype(np.float32))
        assert buf.available() == 500

        buf.clear()
        assert buf.available() == 0
        assert buf.free_space() == 1024
        assert buf.fill_level() == pytest.approx(0.0)

    def test_clear_then_read_returns_silence(self) -> None:
        buf = RingBuffer(100, channels=2)
        buf.write(np.ones((50, 2), dtype=np.float32))
        buf.clear()

        result = buf.read(10)
        np.testing.assert_array_equal(result, np.zeros((10, 2), dtype=np.float32))

    def test_empty_buffer_read(self) -> None:
        buf = RingBuffer(100, channels=2)
        result = buf.read(10)
        assert result.shape == (10, 2)
        np.testing.assert_array_equal(result, np.zeros((10, 2), dtype=np.float32))

    def test_write_zero_frames(self) -> None:
        buf = RingBuffer(100, channels=2)
        written = buf.write(np.zeros((0, 2), dtype=np.float32))
        assert written == 0
        assert buf.available() == 0

    def test_full_buffer_write_returns_zero(self) -> None:
        buf = RingBuffer(100, channels=2)
        buf.write(np.zeros((100, 2), dtype=np.float32))
        written = buf.write(np.ones((10, 2), dtype=np.float32))
        assert written == 0


class TestRingBufferThreadSafety:
    """Test concurrent access from multiple threads."""

    def test_concurrent_write_read(self) -> None:
        buf = RingBuffer(10000, channels=2)
        total_frames = 5000
        chunk_size = 50
        errors = []

        def writer():
            written = 0
            while written < total_frames:
                data = np.ones((chunk_size, 2), dtype=np.float32) * 0.5
                n = buf.write(data)
                written += n
                if n == 0:
                    threading.Event().wait(0.0001)  # Brief yield

        def reader():
            read = 0
            while read < total_frames:
                result = buf.read(chunk_size)
                # Count non-zero frames
                non_zero = np.count_nonzero(result.sum(axis=1))
                read += non_zero
                if non_zero == 0:
                    threading.Event().wait(0.0001)  # Brief yield

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=5)
        r.join(timeout=5)

        assert not w.is_alive(), "Writer thread timed out"
        assert not r.is_alive(), "Reader thread timed out"
        assert not errors
