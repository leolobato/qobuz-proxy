"""
Audio backend types and enumerations.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Optional


class PlaybackState(IntEnum):
    """
    Playback state enumeration.

    Values match Qobuz Connect protocol for easy mapping.
    """

    STOPPED = 1  # No playback, position at 0
    PLAYING = 2  # Active playback
    PAUSED = 3  # Paused, position maintained
    LOADING = 4  # Loading/buffering before playback
    ERROR = 5  # Error state (not in protocol, internal use)


class BufferStatus(IntEnum):
    """
    Buffer status enumeration.

    Used to report buffer health to the Qobuz app.
    """

    EMPTY = 0  # Buffer empty/underrun
    LOW = 1  # Buffer running low
    OK = 2  # Buffer healthy
    FULL = 3  # Buffer full


@dataclass
class BackendTrackMetadata:
    """
    Track metadata for audio backends.

    Contains information needed by backends (especially DLNA) to display
    track information and build DIDL-Lite metadata.
    """

    track_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    artwork_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "artwork_url": self.artwork_url,
        }


@dataclass
class BackendInfo:
    """
    Information about an audio backend/device.

    Used for discovery and display purposes.
    """

    backend_type: str  # 'dlna', 'local', etc.
    name: str  # Display name
    device_id: str  # Unique identifier
    ip: Optional[str] = None
    port: Optional[int] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None

    def __str__(self) -> str:
        if self.ip:
            return f"{self.name} ({self.backend_type}) @ {self.ip}:{self.port}"
        return f"{self.name} ({self.backend_type})"
