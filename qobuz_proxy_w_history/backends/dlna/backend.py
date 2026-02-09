"""
DLNA audio backend.

Implements AudioBackend interface for DLNA/UPnP renderers.
"""

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from .client import DLNAClient, DLNAClientError
from .capabilities import (
    DLNACapabilities,
    CapabilityCache,
    parse_protocol_info_sink,
    apply_device_overrides,
    build_protocol_info,
)

if TYPE_CHECKING:
    from .proxy_server import AudioProxyServer

logger = logging.getLogger(__name__)

# State polling interval
STATE_POLL_INTERVAL_SECONDS = 2.0

# Grace period after starting playback to ignore STOPPED state (seconds)
# This prevents false track-ended events while the device is loading
PLAYBACK_START_GRACE_PERIOD_SECONDS = 5.0

# Class-level capability cache (shared across instances)
_capability_cache = CapabilityCache()


class DLNABackend(AudioBackend):
    """
    DLNA/UPnP audio backend.

    Connects to DLNA renderers and controls playback via SOAP commands.
    Uses polling to monitor device state.

    Note: This backend expects URLs to be provided by the Audio Proxy Server.
    It does not handle URL proxying itself.
    """

    def __init__(
        self,
        ip: str,
        port: int = 1400,
        fixed_volume: bool = False,
        name: Optional[str] = None,
    ):
        """
        Initialize DLNA backend.

        Args:
            ip: DLNA device IP address
            port: DLNA device port (default 1400 for Sonos)
            fixed_volume: If True, ignore volume commands
            name: Display name (auto-detected if not provided)
        """
        super().__init__(name or f"DLNA ({ip})")
        self._ip = ip
        self._port = port
        self._fixed_volume = fixed_volume

        self._client: Optional[DLNAClient] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._current_metadata: Optional[BackendTrackMetadata] = None
        self._position_ms: int = 0
        self._duration_ms: int = 0

        # Audio proxy server for URL handling
        self._proxy_server: Optional["AudioProxyServer"] = None

        # Device capabilities
        self._capabilities: Optional[DLNACapabilities] = None

        # Track when playback was started to avoid false track-ended events
        # during device loading/transition period
        self._playback_started_at: float = 0.0

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def set_proxy_server(self, proxy: "AudioProxyServer") -> None:
        """
        Set the audio proxy server for URL proxying.

        When set, all playback URLs will be routed through the proxy,
        which handles Qobuz URL expiration transparently.

        Args:
            proxy: AudioProxyServer instance
        """
        self._proxy_server = proxy
        logger.info("Audio proxy server configured for DLNA backend")

    async def connect(self) -> bool:
        """Connect to DLNA device."""
        try:
            self._client = DLNAClient(self._ip, self._port)
            device_info = await self._client.connect()

            # Update name from device
            if device_info.friendly_name:
                self.name = device_info.friendly_name

            # Query device capabilities
            await self._discover_capabilities(device_info)

            self._is_connected = True

            # Start state polling
            self._poll_task = asyncio.create_task(self._poll_state_loop())

            logger.info(f"Connected to DLNA device: {self.name}")
            return True

        except DLNAClientError as e:
            logger.error(f"Failed to connect to DLNA device: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to DLNA: {e}", exc_info=True)
            return False

    async def _discover_capabilities(self, device_info) -> None:
        """Query and parse device capabilities."""
        # Check cache first
        device_id = device_info.udn or self._ip
        cached = _capability_cache.get(device_id)
        if cached:
            logger.debug(f"Using cached capabilities for {device_id}")
            self._capabilities = cached
            return

        # Query GetProtocolInfo
        try:
            if not self._client:
                return
            sink = await self._client.get_protocol_info()
            if sink:
                self._capabilities = parse_protocol_info_sink(sink)
                # Apply device-specific overrides
                apply_device_overrides(
                    self._capabilities,
                    device_info.manufacturer,
                    device_info.model_name,
                )
                # Cache the result
                _capability_cache.set(device_id, self._capabilities)
            else:
                logger.debug("GetProtocolInfo not supported, using defaults")
                self._capabilities = None
        except Exception as e:
            logger.warning(f"Failed to discover capabilities: {e}")
            self._capabilities = None

    async def disconnect(self) -> None:
        """Disconnect from DLNA device."""
        self._is_connected = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._client:
            # Stop playback before disconnecting
            try:
                await self._client.stop()
            except Exception:
                pass
            await self._client.disconnect()

        logger.info(f"Disconnected from DLNA device: {self.name}")

    # =========================================================================
    # Playback Control
    # =========================================================================

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Start playback of track."""
        if not self._client:
            raise RuntimeError("Not connected")

        self._current_metadata = metadata
        self._duration_ms = metadata.duration_ms

        # Determine content type from URL or default to FLAC
        content_type = "audio/flac"
        if ".mp3" in url.lower() or "format=5" in url.lower():
            content_type = "audio/mpeg"

        # Register with proxy server if available
        actual_url = url
        if self._proxy_server:
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id,
                qobuz_url=url,
                content_type=content_type,
            )
            logger.debug(f"Using proxy URL: {actual_url}")

        # Build DIDL-Lite metadata
        didl = self._build_didl(actual_url, metadata, content_type)

        # Set URI and start playback
        if await self._client.set_av_transport_uri(actual_url, didl):
            if await self._client.play():
                self._position_ms = 0
                self._playback_started_at = time.monotonic()
                self._notify_state_change(PlaybackState.PLAYING)
                logger.info(f"Playing: {metadata.artist} - {metadata.title}")
            else:
                self._notify_playback_error("Failed to start playback")
        else:
            self._notify_playback_error("Failed to set transport URI")

    async def pause(self) -> None:
        """Pause playback."""
        if self._client and await self._client.pause():
            self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> None:
        """Resume playback."""
        if self._client and await self._client.play():
            self._notify_state_change(PlaybackState.PLAYING)

    async def stop(self) -> None:
        """Stop playback."""
        if self._client and await self._client.stop():
            self._position_ms = 0
            self._playback_started_at = 0.0  # Clear grace period
            self._notify_state_change(PlaybackState.STOPPED)

    # =========================================================================
    # Position Control
    # =========================================================================

    async def seek(self, position_ms: int) -> None:
        """Seek to position."""
        if self._client and await self._client.seek(position_ms):
            self._position_ms = position_ms
            self._notify_position_update(position_ms)

    async def get_position(self) -> int:
        """Get current position."""
        if self._client:
            pos = await self._client.get_position_info()
            if pos is not None:
                self._position_ms = pos
                logger.debug(f"DLNA position: {pos}ms")
            else:
                logger.debug("DLNA position: None returned")
        return self._position_ms

    # =========================================================================
    # Volume Control
    # =========================================================================

    async def set_volume(self, level: int) -> None:
        """Set volume (0-100)."""
        if self._fixed_volume:
            logger.debug("Fixed volume mode: ignoring set_volume")
            return

        clamped = max(0, min(100, level))
        if self._client:
            await self._client.set_volume(clamped)
            self._volume = clamped

    async def get_volume(self) -> int:
        """Get current volume (0-100)."""
        if self._fixed_volume:
            return 100

        if self._client:
            vol = await self._client.get_volume()
            if vol is not None:
                self._volume = vol
        return self._volume

    # =========================================================================
    # State
    # =========================================================================

    async def get_state(self) -> PlaybackState:
        """Get current playback state from device."""
        if not self._client:
            return PlaybackState.STOPPED

        state_str = await self._client.get_transport_info()
        if state_str:
            if state_str == "PLAYING":
                return PlaybackState.PLAYING
            elif state_str == "PAUSED_PLAYBACK":
                return PlaybackState.PAUSED
            elif state_str == "TRANSITIONING":
                return PlaybackState.LOADING

        return PlaybackState.STOPPED

    async def get_buffer_status(self) -> BufferStatus:
        """Get buffer status (always OK for DLNA)."""
        return BufferStatus.OK

    # =========================================================================
    # Info
    # =========================================================================

    def get_info(self) -> BackendInfo:
        """Get backend information."""
        info = BackendInfo(
            backend_type="dlna",
            name=self.name,
            device_id=(
                self._client.device_info.udn if self._client and self._client.device_info else ""
            ),
            ip=self._ip,
            port=self._port,
        )
        if self._client and self._client.device_info:
            info.model = self._client.device_info.model_name
            info.manufacturer = self._client.device_info.manufacturer
        return info

    # =========================================================================
    # Capabilities
    # =========================================================================

    def get_capabilities(self) -> Optional[DLNACapabilities]:
        """
        Get discovered device capabilities.

        Returns:
            DLNACapabilities if discovered, None otherwise
        """
        return self._capabilities

    def get_recommended_quality(self) -> Optional[int]:
        """
        Get recommended Qobuz quality level based on device capabilities.

        Returns:
            Qobuz quality level (5, 6, 7, or 27), or None if not available
        """
        if self._capabilities:
            return self._capabilities.max_quality
        return None

    # =========================================================================
    # Internal
    # =========================================================================

    async def _poll_state_loop(self) -> None:
        """Poll device state periodically."""
        while self._is_connected:
            try:
                await asyncio.sleep(STATE_POLL_INTERVAL_SECONDS)

                if not self._is_connected:
                    break

                # Get state from device
                new_state = await self.get_state()

                # Check if we're in the grace period after starting playback
                in_grace_period = (
                    time.monotonic() - self._playback_started_at
                    < PLAYBACK_START_GRACE_PERIOD_SECONDS
                )

                # Detect state changes
                if new_state != self._state:
                    logger.debug(f"State changed: {self._state} -> {new_state}")

                    # Check for track end before updating state
                    if self._state == PlaybackState.PLAYING and new_state == PlaybackState.STOPPED:
                        if in_grace_period:
                            # During grace period, ignore STOPPED state entirely
                            # This prevents false track-ended events while device is loading
                            logger.debug(
                                f"Ignoring STOPPED state during grace period "
                                f"(started {time.monotonic() - self._playback_started_at:.1f}s ago)"
                            )
                            continue  # Skip state update entirely
                        else:
                            self._notify_track_ended()

                    self._notify_state_change(new_state)

                # Update position while playing
                if new_state == PlaybackState.PLAYING:
                    pos = await self.get_position()
                    self._notify_position_update(pos)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"State poll error: {e}")

    def _build_didl(
        self,
        url: str,
        metadata: BackendTrackMetadata,
        content_type: str = "audio/flac",
    ) -> str:
        """Build DIDL-Lite metadata XML."""

        def escape(s: str) -> str:
            return (
                s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )

        # Build protocol info string based on capabilities
        if self._capabilities:
            protocol_info = build_protocol_info(self._capabilities, content_type)
        else:
            protocol_info = f"http-get:*:{content_type}:*"

        didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
    <item id="1" parentID="0" restricted="1">
        <dc:title>{escape(metadata.title)}</dc:title>
        <dc:creator>{escape(metadata.artist)}</dc:creator>
        <upnp:artist>{escape(metadata.artist)}</upnp:artist>
        <upnp:album>{escape(metadata.album)}</upnp:album>
        <upnp:class>object.item.audioItem.musicTrack</upnp:class>"""

        if metadata.artwork_url:
            didl += f"\n        <upnp:albumArtURI>{escape(metadata.artwork_url)}</upnp:albumArtURI>"

        didl += f"""
        <res protocolInfo="{escape(protocol_info)}">{escape(url)}</res>
    </item>
</DIDL-Lite>"""

        return didl
