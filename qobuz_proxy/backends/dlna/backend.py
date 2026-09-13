"""
DLNA audio backend.

Implements AudioBackend interface for DLNA/UPnP renderers.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import (
    BackendInfo,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from .client import DLNAClient, DLNAClientError, SoapResult
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

# Real STOPPED must be seen this many polls in a row before we treat the
# track as finished. get_transport_info() is None on a Wi-Fi blip, and older
# renderers also flap STOPPED while loading; one bad poll must not skip.
PLAYBACK_STOP_CONFIRMATIONS = 2

# Consecutive failed transport polls (after SOAP retries) before tearing
# the speaker down so mDNS stops advertising and boot-style reconnect runs.
RENDERER_UNREACHABLE_POLLS = 2

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
        description_url: Optional[str] = None,
    ):
        """
        Initialize DLNA backend.

        Args:
            ip: DLNA device IP address
            port: DLNA device port (default 1400 for Sonos)
            fixed_volume: If True, ignore volume commands
            name: Display name (auto-detected if not provided)
            description_url: Full URL to UPnP device description XML
        """
        super().__init__(name or f"DLNA ({ip})")
        self._ip = ip
        self._port = port
        self._fixed_volume = fixed_volume
        self._description_url = description_url

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

        # Gapless playback state
        self._next_track_proxy_url: Optional[str] = None
        self._next_track_metadata: Optional[BackendTrackMetadata] = None
        self._next_track_queue_nr: Optional[int] = None  # Sonos queue position of armed track
        self._gapless_supported: bool = True
        self._current_proxy_url: Optional[str] = None

        # Sonos queue-based playback (for Sonos app metadata display)
        self._is_sonos: bool = False
        self._external_playback = False
        self._starting_playback = False
        self._on_external_playback: Optional[Callable[[], Awaitable[None]]] = None
        self._on_renderer_unreachable: Optional[Callable[[], Awaitable[None]]] = None
        self._unreachable_polls: int = 0
        self._unreachable_notified: bool = False
        self._unreachable_task: Optional[asyncio.Task] = None
        self._stopped_polls: int = 0

    def on_external_playback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback for another source taking over the renderer."""
        self._on_external_playback = callback

    def on_renderer_unreachable(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback when the renderer stays unreachable over Wi-Fi."""
        self._on_renderer_unreachable = callback

    def prepare_for_selection(self) -> None:
        """Allow a fresh Qobuz selection to load audio after an external takeover."""
        if self._external_playback:
            self._current_proxy_url = None
            self._external_playback = False

    async def check_external_playback(self) -> bool:
        """Detect source changes even when the cached Qobuz state is paused."""
        await self._owns_transport()
        return self._external_playback

    async def can_apply_remote_state(self) -> bool:
        """Require source confirmation for snapshots of an already loaded transport."""
        if self._current_proxy_url:
            return await self._owns_transport()
        return not self._external_playback

    async def _owns_transport(self) -> bool:
        """Only control the current or armed Qobuz track; unknown sources fail closed."""
        if (
            self._external_playback
            or self._starting_playback
            or not self._client
            or not self._current_proxy_url
        ):
            return False
        expected = self._current_proxy_url
        current_uri = (
            await self._client.get_track_uri()
            if self._is_sonos
            else await self._client.get_media_info()
        )
        # A concurrent explicit play may have replaced the track during the read.
        if (
            self._external_playback
            or self._starting_playback
            or expected != self._current_proxy_url
            or not current_uri
        ):
            return False
        if current_uri in (self._current_proxy_url, self._next_track_proxy_url):
            return True
        # Renderers can briefly report the old source while loading our URI.
        if time.monotonic() - self._playback_started_at < PLAYBACK_START_GRACE_PERIOD_SECONDS:
            return False
        self._external_playback = True
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None
        logger.info("[%s] External source took over; releasing Qobuz playback control", self.name)
        if self._on_external_playback:
            await self._on_external_playback()
        return False

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
            self._client = DLNAClient(self._ip, self._port, description_url=self._description_url)
            device_info = await self._client.connect()

            # Update name from device
            if device_info.friendly_name:
                self.name = device_info.friendly_name

            # Detect Sonos devices for queue-based playback
            self._is_sonos = "sonos" in (device_info.manufacturer or "").lower()
            if self._is_sonos:
                logger.info("Sonos device detected — using queue-based playback")

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

        if self._unreachable_task and not self._unreachable_task.done():
            task = self._unreachable_task
            self._unreachable_task = None
            # Don't cancel ourselves: this disconnect is often invoked from
            # the unreachable handler running on that same task.
            if task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._client:
            # Stop playback before disconnecting
            try:
                await self.stop()
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
        if await self.check_external_playback():
            raise RuntimeError("External source owns renderer; select it again in Qobuz")
        self._starting_playback = True
        try:
            await self._play(url, metadata)
        finally:
            self._starting_playback = False

    async def _play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Load audio while source polling is suspended during the transport change."""

        # Clear gapless state — explicit play invalidates armed next track
        # (no queue removal needed: Sonos play clears the whole queue)
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

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

        # Start playback — Sonos uses queue for proper app metadata display
        if self._is_sonos:
            success = await self._play_via_queue(actual_url, didl)
        else:
            success = await self._play_via_transport(actual_url, didl)

        if success:
            self._external_playback = False
            self._position_ms = 0
            self._current_proxy_url = actual_url
            self._playback_started_at = time.monotonic()
            self._unreachable_polls = 0
            self._unreachable_notified = False
            self._stopped_polls = 0
            self._notify_state_change(PlaybackState.PLAYING)
            logger.info(f"Playing: {metadata.artist} - {metadata.title}")

    async def _play_via_transport(self, url: str, didl: str) -> bool:
        """Start playback using SetAVTransportURI (standard DLNA).

        If the first attempt fails (e.g. the renderer is wedged after a rapid
        track switch), recover the connection and reset the device transport,
        then retry once before reporting an error. This lets a stuck renderer
        recover without restarting the addon or power-cycling the device.
        """
        assert self._client
        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            return True

        logger.warning(f"Playback {stage} failed; attempting transport recovery")
        # Recover the HTTP session and clear the device's stuck transport.
        await self._client.reset_session()
        try:
            await self._client.stop()
        except Exception:
            pass

        stage = await self._try_transport_sequence(url, didl)
        if stage is None:
            logger.info("Transport recovery succeeded")
            return True

        if stage == "play":
            self._notify_playback_error("Failed to start playback")
        else:
            self._notify_playback_error("Failed to set transport URI")
        return False

    async def _try_transport_sequence(self, url: str, didl: str) -> Optional[str]:
        """Run SetAVTransportURI + Play once.

        Returns None on success, or the name of the failed stage
        ("set_uri" or "play").
        """
        assert self._client
        if not await self._client.set_av_transport_uri(url, didl):
            return "set_uri"
        if not await self._client.play():
            return "play"
        return None

    async def _play_via_queue(self, url: str, didl: str) -> bool:
        """Start playback using Sonos queue (shows metadata in Sonos app)."""
        assert self._client
        if not await self._client.clear_queue():
            logger.warning("Failed to clear queue, falling back to transport URI")
            return await self._play_via_transport(url, didl)

        if not await self._client.add_uri_to_queue(url, didl):
            logger.warning("Failed to add to queue, falling back to transport URI")
            return await self._play_via_transport(url, didl)

        if await self._client.play_from_queue(0):
            return True

        self._notify_playback_error("Failed to play from queue")
        return False

    async def pause(self) -> None:
        """Pause playback."""
        if self._client and await self._owns_transport() and await self._client.pause():
            self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        """Resume playback.

        Returns True only when the renderer accepted the play command — a
        failed SOAP call must not be reported as a successful resume.
        """
        if self._client and await self._owns_transport() and await self._client.play():
            self._notify_state_change(PlaybackState.PLAYING)
            return True
        return False

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        """Stop playback."""
        owns_transport = await self._owns_transport()
        # Clear gapless state
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

        if self._client and owns_transport and await self._client.stop():
            self._position_ms = 0
            self._playback_started_at = 0.0  # Clear grace period
            self._notify_state_change(PlaybackState.STOPPED)

    # =========================================================================
    # Position Control
    # =========================================================================

    async def seek(self, position_ms: int) -> None:
        """Seek to position."""
        if not await self._owns_transport():
            raise RuntimeError("Cannot seek: renderer is not playing the Qobuz track")
        if self._client and await self._client.seek(position_ms):
            self._position_ms = position_ms
            self._notify_position_update(position_ms)

    async def get_position(self) -> int:
        """Get current position."""
        if self._client and await self._owns_transport():
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
        if await self.check_external_playback():
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
        """Get current playback state from device.

        A failed SOAP read is not STOPPED — that used to look like end-of-track
        on a Wi-Fi blip. Keep the last known state until a real transport
        string arrives.
        """
        resolved = await self._read_transport_state()
        if resolved is None:
            return self._state
        return resolved

    async def _read_transport_state(self) -> Optional[PlaybackState]:
        """Map GetTransportInfo to a state, or None when the renderer is unreachable."""
        if not self._client:
            return None
        try:
            state_str = await self._client.get_transport_info()
        except Exception:
            return None
        if not state_str:
            return None
        if state_str == "PLAYING":
            return PlaybackState.PLAYING
        if state_str == "PAUSED_PLAYBACK":
            return PlaybackState.PAUSED
        if state_str == "TRANSITIONING":
            return PlaybackState.LOADING
        if state_str in ("STOPPED", "NO_MEDIA_PRESENT"):
            return PlaybackState.STOPPED
        # Unrecognized strings still count as STOPPED so a quirky renderer
        # ends the track rather than hanging forever. Track-end requires
        # consecutive confirmations in the poll loop.
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

    @property
    def quality_detection_confirmed(self) -> bool:
        """Whether the recommended quality came from explicit device format info.

        False means the device advertised FLAC without stating sample rates or
        bit depths, so the recommendation is just a conservative CD default.
        """
        return self._capabilities is not None and self._capabilities.format_info_confirmed

    # =========================================================================
    # Gapless Playback
    # =========================================================================

    @property
    def supports_gapless(self) -> bool:
        """Whether this backend supports gapless playback."""
        return self._gapless_supported

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Prepare the next track for gapless transition."""
        if not self._client or not self._gapless_supported or not await self._owns_transport():
            return False

        # Determine content type
        content_type = "audio/flac"
        if ".mp3" in url.lower() or "format=5" in url.lower():
            content_type = "audio/mpeg"

        # Register with proxy server using unique key
        actual_url = url
        if self._proxy_server:
            proxy_key = f"{metadata.track_id}_{queue_item_id}"
            actual_url = self._proxy_server.register_track(
                track_id=metadata.track_id,
                qobuz_url=url,
                content_type=content_type,
                proxy_key=proxy_key,
            )
            logger.debug(f"Gapless: registered next track proxy URL: {actual_url}")

        # Build DIDL-Lite metadata
        didl = self._build_didl(actual_url, metadata, content_type)

        # Send to device — Sonos uses queue, others use SetNextAVTransportURI
        if self._is_sonos:
            # Already armed with this URL — appending again would queue a
            # duplicate entry and make the song play twice
            if self._next_track_proxy_url == actual_url and self._next_track_queue_nr is not None:
                logger.debug("Gapless: next track already armed, skipping duplicate")
                return True
            queue_nr = await self._client.add_uri_to_queue(actual_url, didl)
            if queue_nr is not None:
                self._next_track_proxy_url = actual_url
                self._next_track_metadata = metadata
                self._next_track_queue_nr = queue_nr
                logger.info(f"Gapless: armed next track: {metadata.artist} - {metadata.title}")
                return True
            logger.warning("Gapless: failed to add next track to queue")
            return False

        result: SoapResult = await self._client.set_next_av_transport_uri(actual_url, didl)

        if result.success:
            self._next_track_proxy_url = actual_url
            self._next_track_metadata = metadata
            logger.info(f"Gapless: armed next track: {metadata.artist} - {metadata.title}")
            return True

        if result.is_permanent_failure:
            self._gapless_supported = False
            logger.warning(
                f"Gapless: disabled — device does not support SetNextAVTransportURI "
                f"(error {result.error_code}: {result.error_description})"
            )
        else:
            logger.warning(
                "Gapless: failed to arm next track (transient error), will retry on next poll cycle"
            )
        return False

    async def clear_next_track(self) -> None:
        """Clear prepared next track.

        On Sonos the armed track was appended to the device queue, so it must
        be removed there too — otherwise it still plays after the current track.
        """
        if (
            self._is_sonos
            and self._client
            and self._next_track_queue_nr is not None
            and await self._owns_transport()
        ):
            await self._client.remove_track_from_queue(self._next_track_queue_nr)
        self._next_track_proxy_url = None
        self._next_track_metadata = None
        self._next_track_queue_nr = None

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

                # Spotify/AirPlay/other controllers own their transport and queue.
                # Their state changes must never trigger Qobuz auto-advance.
                if (
                    self._starting_playback
                    or not self._current_proxy_url
                    or await self.check_external_playback()
                ):
                    continue

                # Get state from device. None means SOAP failed — not STOPPED.
                new_state = await self._read_transport_state()
                if new_state is None:
                    await self._note_unreachable_poll()
                    continue
                self._unreachable_polls = 0

                # Check if we're in the grace period after starting playback
                in_grace_period = (
                    time.monotonic() - self._playback_started_at
                    < PLAYBACK_START_GRACE_PERIOD_SECONDS
                )

                # Gapless transition detection: check if device has moved to next track
                if (
                    new_state == PlaybackState.PLAYING
                    and self._next_track_proxy_url
                    and self._client
                ):
                    # For Sonos queue playback, GetMediaInfo.CurrentURI returns the
                    # queue URI, not the track URL. Use GetPositionInfo.TrackURI instead.
                    if self._is_sonos:
                        current_uri = await self._client.get_track_uri()
                    else:
                        current_uri = await self._client.get_media_info()
                    if current_uri and current_uri == self._next_track_proxy_url:
                        logger.info("Gapless: transition detected — device moved to next track")
                        # Update state to reflect the new track
                        self._current_metadata = self._next_track_metadata
                        self._current_proxy_url = self._next_track_proxy_url
                        if self._next_track_metadata:
                            self._duration_ms = self._next_track_metadata.duration_ms
                        self._position_ms = 0
                        self._playback_started_at = time.monotonic()
                        # Clear gapless state — the armed entry is now the
                        # playing one, so don't remove it from the queue
                        self._next_track_proxy_url = None
                        self._next_track_metadata = None
                        self._next_track_queue_nr = None
                        # Notify player
                        self._notify_next_track_started()
                        self._notify_position_update(0)
                        continue

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

                        self._stopped_polls += 1
                        if self._stopped_polls < PLAYBACK_STOP_CONFIRMATIONS:
                            logger.debug(
                                "Ignoring unconfirmed STOPPED poll "
                                f"({self._stopped_polls}/{PLAYBACK_STOP_CONFIRMATIONS})"
                            )
                            continue

                        self._stopped_polls = 0
                        # If gapless was armed but device stopped, clear and fall through
                        if self._next_track_proxy_url:
                            logger.debug(
                                "Gapless: device stopped despite armed next track, "
                                "falling through to normal track-ended"
                            )
                            self._next_track_proxy_url = None
                            self._next_track_metadata = None
                        self._notify_track_ended()
                    else:
                        self._stopped_polls = 0

                    self._notify_state_change(new_state)
                else:
                    self._stopped_polls = 0

                # Update position while playing
                if new_state == PlaybackState.PLAYING:
                    pos = await self.get_position()
                    self._notify_position_update(pos)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"State poll error: {e}")

    async def _note_unreachable_poll(self) -> None:
        """Count a failed transport read; reconnect after it persists."""
        self._unreachable_polls += 1
        self._stopped_polls = 0
        if self._unreachable_polls == 1:
            logger.warning(
                "[%s] Renderer unreachable — resetting SOAP session",
                self.name,
            )
            if self._client:
                try:
                    await self._client.reset_session()
                except Exception:
                    pass
        else:
            logger.warning(
                "[%s] Renderer still unreachable (%s consecutive poll(s))",
                self.name,
                self._unreachable_polls,
            )
        if (
            self._unreachable_polls >= RENDERER_UNREACHABLE_POLLS
            and not self._unreachable_notified
        ):
            self._unreachable_notified = True
            logger.warning(
                "[%s] Renderer unreachable after %s polls — reconnecting",
                self.name,
                self._unreachable_polls,
            )
            self._unreachable_task = asyncio.create_task(self._emit_renderer_unreachable())

    async def _emit_renderer_unreachable(self) -> None:
        callback = self._on_renderer_unreachable
        if callback is None:
            return
        try:
            await callback()
        except Exception:
            logger.exception("[%s] Renderer unreachable handler failed", self.name)

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

        # Format duration as H:MM:SS for the res element
        duration_attr = ""
        if metadata.duration_ms > 0:
            total_s = metadata.duration_ms // 1000
            h = total_s // 3600
            m = (total_s % 3600) // 60
            s = total_s % 60
            duration_attr = f' duration="{h}:{m:02d}:{s:02d}"'

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

        audio_attrs = ""
        if metadata.sample_rate:
            audio_attrs += f' sampleFrequency="{metadata.sample_rate}"'
        if metadata.bit_depth:
            audio_attrs += f' bitsPerSample="{metadata.bit_depth}"'

        didl += f"""
        <res protocolInfo="{escape(protocol_info)}"{duration_attr}{audio_attrs}>{escape(url)}</res>
    </item>
</DIDL-Lite>"""

        return didl
