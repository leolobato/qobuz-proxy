"""
Audio output stream wrapper.

Manages a sounddevice OutputStream that reads from a RingBuffer
and outputs to a PortAudio device.
"""

import logging
import threading

import numpy as np

from .ring_buffer import RingBuffer

logger = logging.getLogger(__name__)


class AudioOutputStream:
    """
    Wraps sounddevice.OutputStream with ring buffer integration.

    The audio callback reads from the ring buffer, applies volume,
    and outputs silence when paused.
    """

    def __init__(
        self,
        device_index: int,
        ring_buffer: RingBuffer,
        blocksize: int = 2048,
    ):
        self._device_index = device_index
        self._ring_buffer = ring_buffer
        self._blocksize = blocksize
        self._stream = None  # sd.OutputStream
        self._sample_rate: int = 0
        self._channels: int = 2
        self._volume: float = 0.5  # 0.0 to 1.0
        self._paused = False
        self._underrun_count = 0
        self._lock = threading.Lock()

    def open(self, sample_rate: int, channels: int = 2) -> None:
        """
        Open the audio stream.

        Closes existing stream if sample rate changed.
        """
        import sounddevice as sd

        if self._stream is not None:
            if self._sample_rate == sample_rate and self._channels == channels:
                return  # Already open at correct rate
            self.close()

        self._sample_rate = sample_rate
        self._channels = channels
        self._underrun_count = 0

        self._stream = sd.OutputStream(
            device=self._device_index,
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=self._blocksize,
            callback=self._audio_callback,
        )
        logger.debug(
            f"Audio stream opened: {sample_rate}Hz, {channels}ch, "
            f"blocksize={self._blocksize}"
        )

    def start(self) -> None:
        """Start the audio stream."""
        if self._stream:
            self._stream.start()

    def stop(self) -> None:
        """Stop the audio stream."""
        if self._stream:
            self._stream.stop()

    def close(self) -> None:
        """Close and release the audio stream."""
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"Error closing audio stream: {e}")
            self._stream = None
            self._sample_rate = 0

    def pause(self) -> None:
        """Pause output (callback outputs silence)."""
        self._paused = True

    def resume(self) -> None:
        """Resume output from ring buffer."""
        self._paused = False

    def set_volume(self, level: int) -> None:
        """Set volume level (0-100)."""
        self._volume = max(0.0, min(1.0, level / 100.0))

    def get_volume(self) -> int:
        """Get volume level (0-100)."""
        return int(self._volume * 100)

    def set_ring_buffer(self, ring_buffer: RingBuffer) -> None:
        """Replace the ring buffer (used when sample rate/channels change per-track)."""
        self._ring_buffer = ring_buffer

    @property
    def sample_rate(self) -> int:
        """Current stream sample rate."""
        return self._sample_rate

    @property
    def is_open(self) -> bool:
        """Check if stream is open."""
        return self._stream is not None

    def _audio_callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        """
        PortAudio callback — called from audio thread.

        Reads from ring buffer, applies volume, outputs silence when paused.
        """
        if status:
            logger.warning(f"Audio callback status: {status}")

        if self._paused:
            outdata[:] = 0
            return

        data = self._ring_buffer.read(frames)

        # Check for underrun
        if self._ring_buffer.available() == 0 and not self._paused:
            self._underrun_count += 1
            if self._underrun_count % 10 == 1:
                logger.warning(f"Audio buffer underrun (count: {self._underrun_count})")

        # Apply volume
        if self._volume < 1.0:
            outdata[:] = data * self._volume
        else:
            outdata[:] = data
