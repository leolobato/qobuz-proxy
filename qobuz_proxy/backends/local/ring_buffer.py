"""
Thread-safe ring buffer for audio samples.

Stores interleaved float32 audio samples in a circular numpy array.
Used by the PortAudio audio callback to read samples for output.
"""

import threading

import numpy as np


class RingBuffer:
    """
    Thread-safe circular buffer for audio samples.

    Stores float32 samples in a numpy array with wrap-around handling.
    """

    def __init__(self, capacity_frames: int, channels: int = 2):
        """
        Initialize ring buffer.

        Args:
            capacity_frames: Maximum number of audio frames to store
            channels: Number of audio channels (default: 2 for stereo)
        """
        self._capacity = capacity_frames
        self._channels = channels
        self._buffer = np.zeros((capacity_frames, channels), dtype=np.float32)
        self._write_pos = 0
        self._read_pos = 0
        self._available = 0
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> int:
        """
        Write audio frames to the buffer.

        Args:
            data: numpy array of shape (frames, channels), dtype float32

        Returns:
            Number of frames actually written (may be less if buffer full)
        """
        with self._lock:
            frames = min(len(data), self._capacity - self._available)
            if frames == 0:
                return 0

            # Handle wrap-around
            end_pos = self._write_pos + frames
            if end_pos <= self._capacity:
                self._buffer[self._write_pos : end_pos] = data[:frames]
            else:
                first_chunk = self._capacity - self._write_pos
                self._buffer[self._write_pos :] = data[:first_chunk]
                self._buffer[: frames - first_chunk] = data[first_chunk:frames]

            self._write_pos = (self._write_pos + frames) % self._capacity
            self._available += frames
            return frames

    def read(self, frames: int) -> np.ndarray:
        """
        Read audio frames from the buffer.

        Returns exactly `frames` samples. Zero-pads if buffer underrun.

        Args:
            frames: Number of frames to read

        Returns:
            numpy array of shape (frames, channels)
        """
        with self._lock:
            output = np.zeros((frames, self._channels), dtype=np.float32)
            actual = min(frames, self._available)

            if actual > 0:
                end_pos = self._read_pos + actual
                if end_pos <= self._capacity:
                    output[:actual] = self._buffer[self._read_pos : end_pos]
                else:
                    first_chunk = self._capacity - self._read_pos
                    output[:first_chunk] = self._buffer[self._read_pos :]
                    output[first_chunk:actual] = self._buffer[: actual - first_chunk]

                self._read_pos = (self._read_pos + actual) % self._capacity
                self._available -= actual

            return output

    def clear(self) -> None:
        """Clear all buffered data."""
        with self._lock:
            self._write_pos = 0
            self._read_pos = 0
            self._available = 0

    def available(self) -> int:
        """Number of frames available for reading."""
        with self._lock:
            return self._available

    def free_space(self) -> int:
        """Number of frames that can be written."""
        with self._lock:
            return self._capacity - self._available

    def fill_level(self) -> float:
        """Buffer fill level as ratio 0.0 to 1.0."""
        with self._lock:
            return self._available / self._capacity if self._capacity > 0 else 0.0

    @property
    def capacity(self) -> int:
        """Total buffer capacity in frames."""
        return self._capacity

    @property
    def channels(self) -> int:
        """Number of audio channels."""
        return self._channels
