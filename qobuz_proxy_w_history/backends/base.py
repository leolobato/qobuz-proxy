"""
Abstract audio backend interface.

Defines the contract that all audio backends must implement.
"""

import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)

logger = logging.getLogger(__name__)

# Event callback types
StateChangeCallback = Callable[[PlaybackState], None]
PositionUpdateCallback = Callable[[int], None]  # position_ms
BufferStatusCallback = Callable[[BufferStatus], None]
TrackEndedCallback = Callable[[], None]
PlaybackErrorCallback = Callable[[str], None]  # error_message


class AudioBackend(ABC):
    """
    Abstract base class for audio output backends.

    Backends must implement all abstract methods. Backends may optionally
    override the default implementations of lifecycle and event methods.

    Two primary backend types are supported:
    - URL-streaming: Backend handles URL (DLNA - passes URL to renderer)
    - Sample-feeding: Backend receives audio samples (local audio - future)

    Phase 1 only implements URL-streaming for DLNA.
    """

    def __init__(self, name: str = "AudioBackend"):
        """Initialize backend."""
        self.name = name
        self._volume: int = 50  # 0-100
        self._state: PlaybackState = PlaybackState.STOPPED
        self._is_connected: bool = False

        # Event callbacks
        self._on_state_change: Optional[StateChangeCallback] = None
        self._on_position_update: Optional[PositionUpdateCallback] = None
        self._on_buffer_status: Optional[BufferStatusCallback] = None
        self._on_track_ended: Optional[TrackEndedCallback] = None
        self._on_playback_error: Optional[PlaybackErrorCallback] = None

    # =========================================================================
    # Playback Control - Required
    # =========================================================================

    @abstractmethod
    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Start playback of a track."""
        pass

    @abstractmethod
    async def pause(self) -> None:
        """Pause current playback."""
        pass

    @abstractmethod
    async def resume(self) -> None:
        """Resume paused playback."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop playback completely."""
        pass

    # =========================================================================
    # Position Control - Required
    # =========================================================================

    @abstractmethod
    async def seek(self, position_ms: int) -> None:
        """Seek to position in current track."""
        pass

    @abstractmethod
    async def get_position(self) -> int:
        """Get current playback position in milliseconds."""
        pass

    # =========================================================================
    # Volume Control - Required
    # =========================================================================

    @abstractmethod
    async def set_volume(self, level: int) -> None:
        """Set playback volume (0-100)."""
        pass

    @abstractmethod
    async def get_volume(self) -> int:
        """Get current volume level (0-100)."""
        pass

    async def set_volume_delta(self, delta: int) -> int:
        """Adjust volume relatively."""
        current = await self.get_volume()
        new_level = max(0, min(100, current + delta))
        await self.set_volume(new_level)
        return new_level

    # =========================================================================
    # State - Required
    # =========================================================================

    @abstractmethod
    async def get_state(self) -> PlaybackState:
        """Get current playback state."""
        pass

    async def get_buffer_status(self) -> BufferStatus:
        """Get buffer status. Default returns OK."""
        return BufferStatus.OK

    # =========================================================================
    # Lifecycle - Required
    # =========================================================================

    @abstractmethod
    async def connect(self) -> bool:
        """Initialize connection to backend. Returns True if successful."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect and clean up backend resources."""
        pass

    def is_connected(self) -> bool:
        """Check if backend is connected."""
        return self._is_connected

    # =========================================================================
    # Event Callbacks
    # =========================================================================

    def on_state_change(self, callback: Optional[StateChangeCallback]) -> None:
        """Register callback for state changes."""
        self._on_state_change = callback

    def on_position_update(self, callback: Optional[PositionUpdateCallback]) -> None:
        """Register callback for position updates."""
        self._on_position_update = callback

    def on_buffer_status(self, callback: Optional[BufferStatusCallback]) -> None:
        """Register callback for buffer status changes."""
        self._on_buffer_status = callback

    def on_track_ended(self, callback: Optional[TrackEndedCallback]) -> None:
        """Register callback for natural track end (not stop command)."""
        self._on_track_ended = callback

    def on_playback_error(self, callback: Optional[PlaybackErrorCallback]) -> None:
        """Register callback for playback errors."""
        self._on_playback_error = callback

    # =========================================================================
    # Event Notification Helpers
    # =========================================================================

    def _notify_state_change(self, state: PlaybackState) -> None:
        """Notify listeners of state change."""
        old_state = self._state
        self._state = state
        if old_state != state and self._on_state_change:
            try:
                self._on_state_change(state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")

    def _notify_position_update(self, position_ms: int) -> None:
        """Notify listeners of position update."""
        if self._on_position_update:
            try:
                self._on_position_update(position_ms)
            except Exception as e:
                logger.error(f"Position update callback error: {e}")

    def _notify_buffer_status(self, status: BufferStatus) -> None:
        """Notify listeners of buffer status change."""
        if self._on_buffer_status:
            try:
                self._on_buffer_status(status)
            except Exception as e:
                logger.error(f"Buffer status callback error: {e}")

    def _notify_track_ended(self) -> None:
        """Notify listeners that track ended naturally."""
        if self._on_track_ended:
            try:
                self._on_track_ended()
            except Exception as e:
                logger.error(f"Track ended callback error: {e}")

    def _notify_playback_error(self, message: str) -> None:
        """Notify listeners of playback error."""
        if self._on_playback_error:
            try:
                self._on_playback_error(message)
            except Exception as e:
                logger.error(f"Playback error callback error: {e}")

    # =========================================================================
    # Info
    # =========================================================================

    def get_info(self) -> BackendInfo:
        """Get information about this backend."""
        return BackendInfo(
            backend_type="unknown",
            name=self.name,
            device_id="",
        )
