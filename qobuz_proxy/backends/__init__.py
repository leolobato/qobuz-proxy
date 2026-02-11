"""
Audio backends module.

Provides abstract interface and factory for audio output backends.
"""

from .base import (
    AudioBackend,
    BufferStatusCallback,
    PlaybackErrorCallback,
    PositionUpdateCallback,
    StateChangeCallback,
    TrackEndedCallback,
)
from .factory import (
    BackendFactory,
    BackendNotFoundError,
    BackendRegistry,
)
from .types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from .dlna import (
    DLNABackend,
    DLNAClient,
    DLNAClientError,
    DLNADeviceInfo,
    AudioProxyServer,
    RegisteredTrack,
    StreamingURLProvider,
    MetadataServiceURLProvider,
)
try:
    from .local import LocalAudioBackend
except ImportError:
    pass  # sounddevice/numpy not installed

__all__ = [
    # Types
    "BackendInfo",
    "BackendTrackMetadata",
    "BufferStatus",
    "PlaybackState",
    # Base class
    "AudioBackend",
    # Callback types
    "BufferStatusCallback",
    "PlaybackErrorCallback",
    "PositionUpdateCallback",
    "StateChangeCallback",
    "TrackEndedCallback",
    # Factory
    "BackendFactory",
    "BackendNotFoundError",
    "BackendRegistry",
    # DLNA backend
    "DLNABackend",
    "DLNAClient",
    "DLNAClientError",
    "DLNADeviceInfo",
    # Audio proxy
    "AudioProxyServer",
    "RegisteredTrack",
    "StreamingURLProvider",
    "MetadataServiceURLProvider",
    # Local backend
    "LocalAudioBackend",
]
