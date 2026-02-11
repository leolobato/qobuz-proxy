"""
Local audio backend.

Downloads FLAC audio from Qobuz, decodes to float32 samples,
and plays through the local audio device via PortAudio.
"""

import asyncio
import io
import logging
from typing import Optional

import aiohttp
import numpy as np

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    PlaybackState,
)
from .device import AudioDeviceInfo, resolve_device
from .ring_buffer import RingBuffer
from .stream import AudioOutputStream

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8192  # Frames per feed iteration
BUFFER_SECONDS = 10  # Ring buffer capacity in seconds
BUFFER_HIGH_WATER = 0.8  # Pause feeding when buffer above this level


class LocalAudioBackend(AudioBackend):
    """Local audio output backend using sounddevice/PortAudio."""

    def __init__(
        self,
        device: str = "default",
        buffer_size: int = 2048,
        name: str = "Local Audio",
    ):
        super().__init__(name)
        self._device_config = device
        self._buffer_size = buffer_size

        # Device and audio components (initialized in connect())
        self._device_info: Optional[AudioDeviceInfo] = None
        self._ring_buffer: Optional[RingBuffer] = None
        self._stream: Optional[AudioOutputStream] = None

        # Playback state
        self._audio_data: Optional[np.ndarray] = None
        self._sample_rate: int = 0
        self._frames_fed: int = 0
        self._total_frames: int = 0
        self._feeding_task: Optional[asyncio.Task] = None

        # Seek support
        self._seek_target: Optional[int] = None

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Download FLAC, decode, and start playback."""
        await self._cancel_feeding()

        self._notify_state_change(PlaybackState.LOADING)

        try:
            audio_data, sample_rate = await self._download_and_decode(url)
            self._audio_data = audio_data
            self._sample_rate = sample_rate
            self._total_frames = len(audio_data)
            self._frames_fed = 0
            self._seek_target = None

            # Create ring buffer for this track's sample rate
            buffer_frames = int(sample_rate * BUFFER_SECONDS)
            channels = audio_data.shape[1] if audio_data.ndim > 1 else 1
            self._ring_buffer = RingBuffer(buffer_frames, channels)

            # Update stream's ring buffer and open/reconfigure
            self._stream.set_ring_buffer(self._ring_buffer)
            self._stream.open(sample_rate, channels)
            self._stream.start()

            # Start feeding loop
            self._feeding_task = asyncio.create_task(self._feeding_loop())
            self._notify_state_change(PlaybackState.PLAYING)

            logger.info(
                f"Playing: {metadata.artist} - {metadata.title} "
                f"({sample_rate}Hz, {self._total_frames} frames)"
            )

        except Exception as e:
            logger.error(f"Playback error: {e}")
            self._notify_state_change(PlaybackState.ERROR)
            self._notify_playback_error(str(e))

    async def _download_and_decode(self, url: str) -> tuple[np.ndarray, int]:
        """Download audio file and decode to float32 numpy array."""
        import soundfile as sf

        logger.debug("Downloading audio from URL...")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.read()

        logger.debug(f"Downloaded {len(data)} bytes, decoding...")
        audio_data, sample_rate = sf.read(io.BytesIO(data), dtype="float32")

        # Ensure 2D array (frames, channels)
        if audio_data.ndim == 1:
            audio_data = audio_data.reshape(-1, 1)

        logger.debug(
            f"Decoded: {len(audio_data)} frames, {audio_data.shape[1]}ch, "
            f"{sample_rate}Hz"
        )
        return audio_data, sample_rate

    async def _feeding_loop(self) -> None:
        """Feed decoded audio to ring buffer in chunks."""
        try:
            while self._frames_fed < self._total_frames:
                # Handle seek
                if self._seek_target is not None:
                    target = self._seek_target
                    self._seek_target = None
                    self._ring_buffer.clear()
                    self._frames_fed = min(target, self._total_frames)
                    if self._frames_fed >= self._total_frames:
                        break
                    continue

                # Pace: wait if buffer is full enough
                if self._ring_buffer.fill_level() > BUFFER_HIGH_WATER:
                    await asyncio.sleep(0.05)
                    continue

                # Feed next chunk
                end = min(self._frames_fed + CHUNK_SIZE, self._total_frames)
                chunk = self._audio_data[self._frames_fed:end]
                written = self._ring_buffer.write(chunk)
                self._frames_fed += written

                # Notify position update
                position_ms = int(self._frames_fed / self._sample_rate * 1000)
                self._notify_position_update(position_ms)

                await asyncio.sleep(0)  # Yield to event loop

            # Wait for buffer to drain
            while self._ring_buffer.available() > 0:
                if self._state == PlaybackState.STOPPED:
                    return
                await asyncio.sleep(0.1)

            # Track ended naturally
            self._notify_state_change(PlaybackState.STOPPED)
            self._notify_track_ended()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Feeding loop error: {e}")
            self._notify_state_change(PlaybackState.ERROR)
            self._notify_playback_error(str(e))

    async def _cancel_feeding(self) -> None:
        """Cancel the current feeding task if running."""
        if self._feeding_task and not self._feeding_task.done():
            self._feeding_task.cancel()
            try:
                await self._feeding_task
            except asyncio.CancelledError:
                pass
        self._feeding_task = None

    async def pause(self) -> None:
        if self._stream:
            self._stream.pause()
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> None:
        if self._stream:
            self._stream.resume()
        self._notify_state_change(PlaybackState.PLAYING)

    async def stop(self) -> None:
        await self._cancel_feeding()
        if self._ring_buffer:
            self._ring_buffer.clear()
        if self._stream:
            self._stream.stop()
        self._frames_fed = 0
        self._audio_data = None
        self._notify_state_change(PlaybackState.STOPPED)

    async def seek(self, position_ms: int) -> None:
        if self._sample_rate > 0:
            target_frame = int(position_ms / 1000 * self._sample_rate)
            self._seek_target = target_frame

    async def get_position(self) -> int:
        if self._sample_rate == 0:
            return 0
        return int(self._frames_fed / self._sample_rate * 1000)

    async def set_volume(self, level: int) -> None:
        self._volume = max(0, min(100, level))
        if self._stream:
            self._stream.set_volume(level)

    async def get_volume(self) -> int:
        return self._volume

    async def get_state(self) -> PlaybackState:
        return self._state

    async def connect(self) -> bool:
        """Initialize connection — resolve device and create audio stream."""
        try:
            self._device_info = resolve_device(self._device_config)
            self.name = f"Local: {self._device_info.name}"

            # Create audio output stream (not opened until play)
            self._stream = AudioOutputStream(
                device_index=self._device_info.index,
                ring_buffer=RingBuffer(1, 2),  # Placeholder, replaced per-track
                blocksize=self._buffer_size,
            )

            self._is_connected = True
            logger.info(
                f"Audio output device: {self._device_info.name} "
                f"({int(self._device_info.default_samplerate)} Hz, "
                f"{self._device_info.channels}ch)"
            )
            return True
        except (ValueError, ImportError) as e:
            logger.error(f"Failed to initialize audio device: {e}")
            return False

    async def disconnect(self) -> None:
        await self.stop()
        if self._stream:
            self._stream.close()
            self._stream = None
        self._is_connected = False

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            backend_type="local",
            name=self.name,
            device_id=f"local-{self._device_config}",
        )
