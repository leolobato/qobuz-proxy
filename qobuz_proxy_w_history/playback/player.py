"""
QobuzProxy Player.

Core playback controller that orchestrates queue, metadata, and audio backend.
"""

import asyncio
import logging
import time
from typing import Callable, Optional, TYPE_CHECKING

from qobuz_proxy.backends import (
    AudioBackend,
    BackendTrackMetadata,
    PlaybackState,
    BufferStatus,
)
from .queue import QobuzQueue, QueueTrack, RepeatMode
from .metadata import MetadataService

if TYPE_CHECKING:
    from .state_reporter import StateReporter

logger = logging.getLogger(__name__)

# Threshold for restart vs previous track (milliseconds)
PREVIOUS_TRACK_THRESHOLD_MS = 3000


class QobuzPlayer:
    """
    Main playback controller.

    Coordinates:
    - Queue: Track ordering, shuffle, repeat
    - MetadataService: Track info and streaming URLs
    - AudioBackend: Actual audio playback
    - WsManager: State reporting to app

    State machine:
        STOPPED -> LOADING (on play)
        LOADING -> PLAYING (when ready)
        LOADING -> ERROR (on failure)
        PLAYING -> PAUSED (on pause)
        PAUSED -> PLAYING (on play/resume)
        PLAYING -> STOPPED (on stop or track end)
        PAUSED -> STOPPED (on stop)
    """

    def __init__(
        self,
        queue: QobuzQueue,
        metadata_service: MetadataService,
        backend: AudioBackend,
    ):
        """Initialize player."""
        self.queue = queue
        self.metadata = metadata_service
        self.backend = backend

        # Current track
        self._current_track: Optional[QueueTrack] = None
        self._current_duration_ms: int = 0

        # Position tracking (timestamp-based like C++ implementation)
        self._position_timestamp_ms: int = 0
        self._position_value_ms: int = 0

        # State
        self._state: PlaybackState = PlaybackState.STOPPED

        # State reporting - supports both callback and StateReporter
        self._state_update_callback: Optional[Callable[[], asyncio.Future]] = None
        self._state_reporter: Optional["StateReporter"] = None

        # Volume
        self._volume: int = 50  # Cached volume level (0-100)
        self._fixed_volume: bool = False  # From config
        self._volume_report_callback: Optional[Callable[[int], asyncio.Future]] = None

        # File quality report callback - called when track starts playing
        self._file_quality_report_callback: Optional[Callable[[int], asyncio.Future]] = None

        # Next track callback - used when track ends to get the next track from SET_STATE
        self._get_next_track_callback: Optional[Callable[[], Optional[dict]]] = None
        self._clear_next_track_callback: Optional[Callable[[], None]] = None

        # Background tasks
        self._playback_monitor_task: Optional[asyncio.Task] = None
        self._state_update_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

        # Wire up queue callbacks to metadata service
        self.queue.set_url_callback(self._get_track_url)
        self.queue.set_metadata_callback(self._get_track_metadata)

        # Wire up backend callbacks
        self.backend.on_track_ended(self._on_track_ended)
        self.backend.on_playback_error(self._on_playback_error)
        self.backend.on_position_update(self._on_position_update)

        logger.info("QobuzPlayer initialized")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the player and its components."""
        if self._is_running:
            return

        self._is_running = True

        # Start queue preloading
        await self.queue.start()

        # Connect backend
        if not self.backend.is_connected():
            await self.backend.connect()

        # Start background tasks
        self._playback_monitor_task = asyncio.create_task(self._playback_monitor_loop())
        self._state_update_task = asyncio.create_task(self._state_update_loop())

        logger.info("Player started")

    async def stop(self) -> None:
        """Stop the player and clean up."""
        self._is_running = False

        # Cancel background tasks
        for task in [self._playback_monitor_task, self._state_update_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop queue
        await self.queue.stop()

        # Disconnect backend
        await self.backend.disconnect()

        logger.info("Player stopped")

    def set_state_update_callback(self, callback: Callable[[], asyncio.Future]) -> None:
        """Set callback to send state updates to app (legacy method)."""
        self._state_update_callback = callback

    def set_state_reporter(self, reporter: "StateReporter") -> None:
        """
        Set the StateReporter for this player.

        When set, the StateReporter handles all state reporting including
        the periodic heartbeat and immediate updates.
        """
        self._state_reporter = reporter

    def set_volume_report_callback(self, callback: Callable[[int], asyncio.Future]) -> None:
        """Set callback to report volume changes to app."""
        self._volume_report_callback = callback

    def set_file_quality_report_callback(self, callback: Callable[[int], asyncio.Future]) -> None:
        """Set callback to report file quality when track starts playing."""
        self._file_quality_report_callback = callback

    def set_fixed_volume_mode(self, enabled: bool) -> None:
        """Enable or disable fixed volume mode."""
        self._fixed_volume = enabled
        logger.info(f"Fixed volume mode: {enabled}")

    def set_next_track_callbacks(
        self,
        get_callback: Callable[[], Optional[dict]],
        clear_callback: Callable[[], None],
    ) -> None:
        """
        Set callbacks for getting next track info from command handler.

        This is used for auto-advance when the current track ends.
        The get_callback should return track info dict with queueItemId and trackId,
        or None if no next track is available.
        """
        self._get_next_track_callback = get_callback
        self._clear_next_track_callback = clear_callback

    # =========================================================================
    # Volume Controls
    # =========================================================================

    async def set_volume(self, level: int) -> int:
        """
        Set absolute volume level.

        Args:
            level: Volume level (0-100), will be clamped to valid range

        Returns:
            Actual volume level after clamping
        """
        # Clamp to valid range
        clamped = max(0, min(100, level))

        if self._fixed_volume:
            logger.debug(f"Fixed volume mode: ignoring set_volume({level})")
            return self._volume  # Return current (ignored)

        # Apply to backend
        await self.backend.set_volume(clamped)
        self._volume = clamped

        # Report change to app
        await self._report_volume_change()

        logger.info(f"Volume set to {clamped}")
        return clamped

    async def set_volume_delta(self, delta: int) -> int:
        """
        Adjust volume by relative amount.

        Args:
            delta: Amount to adjust (+/- value)

        Returns:
            New volume level after adjustment
        """
        current = await self.get_volume()
        new_level = current + delta
        return await self.set_volume(new_level)

    async def get_volume(self) -> int:
        """
        Get current volume level.

        Returns:
            Volume level (0-100)
        """
        if self._fixed_volume:
            return 100  # Fixed volume always reports 100

        # Get from backend (authoritative source)
        self._volume = await self.backend.get_volume()
        return self._volume

    async def _report_volume_change(self) -> None:
        """Send volume change notification to app."""
        if not self._volume_report_callback:
            return

        try:
            await self._volume_report_callback(self._volume)
        except Exception as e:
            logger.error(f"Failed to report volume change: {e}")

    # =========================================================================
    # Seek Control
    # =========================================================================

    async def seek(self, position_ms: int) -> bool:
        """
        Seek to position in current track.

        Args:
            position_ms: Target position in milliseconds

        Returns:
            True if seek successful, False if rejected (no track loaded)
        """
        # Reject if no track loaded
        if self._state == PlaybackState.STOPPED or not self._current_track:
            logger.warning("Cannot seek: no track loaded")
            return False

        # Get track duration
        duration = self._current_duration_ms
        if duration <= 0:
            logger.warning("Cannot seek: unknown track duration")
            return False

        # Clamp position to valid range
        # Leave 1 second buffer at end to avoid triggering track end
        max_position = max(0, duration - 1000)
        clamped_position = max(0, min(position_ms, max_position))

        if clamped_position != position_ms:
            logger.debug(f"Seek position clamped: {position_ms}ms -> {clamped_position}ms")

        logger.info(f"Seeking to {clamped_position}ms (duration: {duration}ms)")

        try:
            # Send seek to backend
            await self.backend.seek(clamped_position)

            # Update position tracking
            self._set_position(clamped_position)

            # Send state update (immediate, not waiting for heartbeat)
            await self._send_state_update()

            logger.info(f"Seek complete to {clamped_position}ms")
            return True

        except Exception as e:
            logger.error(f"Seek failed: {e}", exc_info=True)
            return False

    async def seek_seconds(self, position_seconds: float) -> bool:
        """
        Seek to position in seconds (convenience method).

        Args:
            position_seconds: Target position in seconds

        Returns:
            True if seek successful
        """
        position_ms = int(position_seconds * 1000)
        return await self.seek(position_ms)

    # =========================================================================
    # Playback Commands
    # =========================================================================

    async def play(self, position_ms: int = 0) -> bool:
        """
        Start or resume playback.

        Args:
            position_ms: Optional starting position (only used when starting new playback)

        Returns:
            True if playback started/resumed successfully
        """
        logger.debug(f"Play command, current state: {self._state}")

        # Resume from pause
        if self._state == PlaybackState.PAUSED:
            await self.backend.resume()
            self._state = PlaybackState.PLAYING
            self._position_timestamp_ms = int(time.time() * 1000)
            await self._send_state_update()
            logger.info("Playback resumed")
            return True

        # Already playing
        if self._state == PlaybackState.PLAYING:
            logger.debug("Already playing")
            return True

        # Get track to play (if not already loaded)
        if not self._current_track:
            track = await self.queue.get_current_track()
            if not track:
                track = await self.queue.advance_to_next()
            if not track:
                logger.warning("No track to play - queue empty")
                return False
            self._current_track = track

        # Set starting position
        if position_ms > 0:
            self._position_value_ms = position_ms
            self._position_timestamp_ms = int(time.time() * 1000)

        # Start playback
        success = await self._start_playback()

        # Seek if position > 0 and playback started
        if success and position_ms > 0:
            await self.backend.seek(position_ms)

        return success

    async def pause(self) -> bool:
        """
        Pause playback.

        Returns:
            True if paused successfully
        """
        if self._state != PlaybackState.PLAYING:
            logger.debug(f"Cannot pause in state {self._state}")
            return False

        # Capture position before pausing
        self._position_value_ms = self.current_position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        await self.backend.pause()
        self._state = PlaybackState.PAUSED
        await self._send_state_update()

        logger.info("Playback paused")
        return True

    async def stop_playback(self) -> None:
        """
        Stop playback completely.

        Resets position to 0 but keeps queue position.
        """
        await self.backend.stop()

        self._state = PlaybackState.STOPPED
        self._position_value_ms = 0
        self._position_timestamp_ms = int(time.time() * 1000)

        await self._send_state_update()
        logger.info("Playback stopped")

    async def load_track(
        self,
        queue_item_id: int,
        track_id: str,
    ) -> bool:
        """
        Load a track without starting playback.

        This prepares the track (fetches URL and metadata) so it's ready
        to play immediately when play() is called.

        Args:
            queue_item_id: Queue item identifier
            track_id: Qobuz track ID

        Returns:
            True if track loaded successfully
        """
        logger.info(f"Loading track: track_id={track_id}, queue_item_id={queue_item_id}")

        # Stop current playback if playing
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop()
            self._state = PlaybackState.STOPPED

        # Create track object
        self._current_track = QueueTrack(
            queue_item_id=queue_item_id,
            track_id=track_id,
        )

        # Pre-fetch URL and metadata
        try:
            url = await self._get_track_url(track_id)
            if url:
                self._current_track.streaming_url = url
            else:
                logger.error(f"Failed to get URL for track {track_id}")
                return False

            meta = await self._get_track_metadata(track_id)
            if meta:
                self._current_track.metadata = meta
                self._current_track.duration_ms = meta.get("duration_ms", 0)
                self._current_duration_ms = self._current_track.duration_ms

            logger.info(f"Track loaded: {track_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to load track {track_id}: {e}")
            return False

    async def play_track(
        self,
        queue_item_id: int,
        track_id: str,
        position_ms: int = 0,
    ) -> bool:
        """
        Play a specific track from the queue.

        Args:
            queue_item_id: Queue item identifier
            track_id: Qobuz track ID
            position_ms: Starting position in milliseconds

        Returns:
            True if playback started successfully
        """
        logger.info(
            f"Play track requested: track_id={track_id}, queue_item_id={queue_item_id}, pos={position_ms}ms"
        )

        # Load the track first
        if not await self.load_track(queue_item_id, track_id):
            return False

        # Set starting position
        self._position_value_ms = position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        # Start playback
        success = await self._start_playback()

        # Seek if position > 0 and playback started
        if success and position_ms > 0:
            await self.backend.seek(position_ms)

        return success

    async def set_loop_mode(self, mode: int) -> None:
        """
        Set loop/repeat mode.

        Args:
            mode: Protocol LoopMode - 0=UNKNOWN, 1=OFF, 2=REPEAT_ONE, 3=REPEAT_ALL
        """
        logger.debug(f"Set loop mode: {mode}")
        # Map protocol LoopMode to internal RepeatMode
        # Protocol: 0=UNKNOWN, 1=OFF, 2=REPEAT_ONE, 3=REPEAT_ALL
        # Internal: OFF, ONE, ALL
        mode_map = {
            0: RepeatMode.OFF,  # UNKNOWN -> OFF
            1: RepeatMode.OFF,  # OFF
            2: RepeatMode.ONE,  # REPEAT_ONE
            3: RepeatMode.ALL,  # REPEAT_ALL
        }
        repeat_mode = mode_map.get(mode, RepeatMode.OFF)
        await self.queue.set_repeat_mode(repeat_mode)

    async def set_shuffle_mode(self, enabled: bool) -> None:
        """
        Set shuffle mode.

        Args:
            enabled: True to enable shuffle
        """
        logger.debug(f"Set shuffle mode: {enabled}")
        await self.queue.set_shuffle(enabled)

    async def set_autoplay_mode(self, enabled: bool) -> None:
        """
        Set autoplay mode.

        Args:
            enabled: True to enable autoplay (similar content when queue ends)
        """
        logger.debug(f"Set autoplay mode: {enabled}")
        # Autoplay is handled at queue level - just log for now
        # Full implementation would require fetching similar tracks

    async def next_track(self) -> bool:
        """
        Skip to next track.

        Returns:
            True if advanced to next track, False if at end
        """
        logger.debug("Next track command")

        # Stop current playback
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop()

        # Get next track from queue
        track = await self.queue.advance_to_next()

        if not track:
            # End of queue
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._position_value_ms = 0
            await self._send_state_update()
            logger.info("End of queue - playback stopped")
            return False

        # Start playing next track
        self._current_track = track
        await self._start_playback()
        return True

    async def previous_track(self) -> bool:
        """
        Go to previous track or restart current track.

        - If position > 3 seconds: Restart current track
        - If position <= 3 seconds: Go to previous track

        Returns:
            True if action taken successfully
        """
        logger.debug("Previous track command")

        current_pos = self.current_position_ms

        # Restart if past threshold
        if current_pos > PREVIOUS_TRACK_THRESHOLD_MS:
            logger.debug(
                f"Restarting track (position {current_pos}ms > {PREVIOUS_TRACK_THRESHOLD_MS}ms)"
            )
            await self.backend.seek(0)
            self._position_value_ms = 0
            self._position_timestamp_ms = int(time.time() * 1000)
            await self._send_state_update()
            return True

        # Stop current playback
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop()

        # Get previous track from queue
        track = await self.queue.go_to_previous()

        if not track:
            logger.warning("No previous track")
            return False

        # Start playing previous track
        self._current_track = track
        await self._start_playback()
        return True

    # =========================================================================
    # Internal Playback Management
    # =========================================================================

    async def _start_playback(self) -> bool:
        """
        Start playback of current track.

        Returns:
            True if playback started successfully
        """
        if not self._current_track:
            return False

        track = self._current_track
        logger.info(f"Starting playback: track {track.track_id}")

        # Set loading state
        self._state = PlaybackState.LOADING
        await self._send_state_update()

        try:
            # Get streaming URL if not cached
            url = track.streaming_url
            if not url:
                url = await self._get_track_url(track.track_id)
                if not url:
                    logger.error(f"Failed to get URL for track {track.track_id}")
                    self._state = PlaybackState.ERROR
                    await self._send_state_update()
                    return False
                track.streaming_url = url

            # Get metadata if not cached
            meta: Optional[dict] = track.metadata if track.metadata else None
            if not meta:
                meta = await self._get_track_metadata(track.track_id)
                if meta:
                    track.metadata = meta
                    track.duration_ms = meta.get("duration_ms", 0)

            # Build backend metadata
            backend_meta = BackendTrackMetadata(
                track_id=track.track_id,
                title=(
                    meta.get("title", f"Track {track.track_id}")
                    if meta
                    else f"Track {track.track_id}"
                ),
                artist=meta.get("artist", "") if meta else "",
                album=meta.get("album", "") if meta else "",
                duration_ms=track.duration_ms,
                artwork_url=meta.get("artwork_url", "") if meta else "",
            )

            # Log now playing
            self.metadata.log_now_playing_info(backend_meta)

            # Report file quality if callback is set
            if self._file_quality_report_callback:
                actual_quality = self.metadata.get_track_actual_quality(track.track_id)
                logger.debug(f"Track {track.track_id} actual_quality={actual_quality}")
                if actual_quality:
                    await self._file_quality_report_callback(actual_quality)
                else:
                    logger.debug(f"No actual_quality for track {track.track_id}, skipping file quality report")

            # Start playback on backend
            await self.backend.play(url, backend_meta)

            # Update state
            self._state = PlaybackState.PLAYING
            self._current_duration_ms = track.duration_ms
            self._position_value_ms = 0
            self._position_timestamp_ms = int(time.time() * 1000)

            await self._send_state_update()
            return True

        except Exception as e:
            logger.error(f"Failed to start playback: {e}", exc_info=True)
            self._state = PlaybackState.ERROR
            await self._send_state_update()
            return False

    # =========================================================================
    # Position Tracking
    # =========================================================================

    @property
    def current_position_ms(self) -> int:
        """Get current playback position."""
        if self._state != PlaybackState.PLAYING:
            return self._position_value_ms

        # Calculate elapsed time since last position update
        now_ms = int(time.time() * 1000)
        elapsed = now_ms - self._position_timestamp_ms
        return self._position_value_ms + elapsed

    def _set_position(self, position_ms: int) -> None:
        """Update position tracking."""
        self._position_value_ms = position_ms
        self._position_timestamp_ms = int(time.time() * 1000)
        logger.debug(f"Position set: {position_ms}ms at ts={self._position_timestamp_ms}")

    # =========================================================================
    # Callbacks from Components
    # =========================================================================

    async def _get_track_url(self, track_id: str) -> Optional[str]:
        """Callback for queue to get streaming URL."""
        return await self.metadata.get_streaming_url(track_id)

    async def _get_track_metadata(self, track_id: str) -> Optional[dict]:
        """Callback for queue to get track metadata."""
        meta = await self.metadata.get_metadata(track_id)
        if meta:
            return meta.to_dict()
        return None

    def _on_track_ended(self) -> None:
        """Callback when backend reports track ended naturally."""
        logger.debug("Track ended callback")
        asyncio.create_task(self._handle_track_ended())

    async def _handle_track_ended(self) -> None:
        """Handle natural track end."""
        logger.info("Track ended naturally")

        # Get queue state to check repeat mode
        queue_state = await self.queue.get_state()

        if queue_state.repeat_mode == RepeatMode.ONE:
            # Restart current track
            await self.backend.seek(0)
            self._set_position(0)
            return

        # Try to get next track from command handler (SET_STATE nextQueueItem)
        if self._get_next_track_callback:
            next_track_info = self._get_next_track_callback()
            if next_track_info:
                logger.info(f"Auto-advancing to next track: {next_track_info['trackId']}")
                # Clear the stored next track info since we're using it
                if self._clear_next_track_callback:
                    self._clear_next_track_callback()

                # Load and play the next track
                await self.play_track(
                    queue_item_id=next_track_info["queueItemId"],
                    track_id=next_track_info["trackId"],
                    position_ms=0,
                )
                return

        # No next track available - stop playback
        logger.info("No next track available - playback stopped")
        self._state = PlaybackState.STOPPED
        self._current_track = None
        self._position_value_ms = 0
        await self._send_state_update()

    def _on_playback_error(self, message: str) -> None:
        """Callback when backend reports playback error."""
        logger.error(f"Playback error: {message}")
        self._state = PlaybackState.ERROR
        asyncio.create_task(self._send_state_update())

    def _on_position_update(self, position_ms: int) -> None:
        """Callback when backend reports position update."""
        self._set_position(position_ms)

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _playback_monitor_loop(self) -> None:
        """Monitor playback and handle backend state changes."""
        while self._is_running:
            try:
                await asyncio.sleep(0.5)

                if self._state == PlaybackState.PLAYING:
                    # Poll backend state
                    backend_state = await self.backend.get_state()

                    if backend_state == PlaybackState.STOPPED:
                        # Track finished naturally (handled by callback)
                        pass
                    elif backend_state == PlaybackState.PAUSED:
                        # External pause (e.g., DLNA device)
                        self._state = PlaybackState.PAUSED
                        await self._send_state_update()

                    # Update position from backend
                    position = await self.backend.get_position()
                    self._set_position(position)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Playback monitor error: {e}")
                await asyncio.sleep(1.0)

    async def _state_update_loop(self) -> None:
        """Periodic state updates (heartbeat)."""
        while self._is_running:
            try:
                await asyncio.sleep(5.0)  # 5 second heartbeat like C++

                # Skip if StateReporter is handling heartbeats
                if self._state_reporter:
                    continue

                if self._state == PlaybackState.PLAYING:
                    await self._send_state_update()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"State update loop error: {e}")

    async def _send_state_update(self) -> None:
        """Send state update to app via StateReporter or callback."""
        # Prefer StateReporter if set
        if self._state_reporter:
            try:
                await self._state_reporter.report_now()
            except Exception as e:
                logger.error(f"Failed to send state update via reporter: {e}")
            return

        # Fall back to legacy callback
        if not self._state_update_callback:
            return

        try:
            await self._state_update_callback()
        except Exception as e:
            logger.error(f"Failed to send state update: {e}")

    # =========================================================================
    # State Access
    # =========================================================================

    @property
    def state(self) -> PlaybackState:
        """Get current playback state."""
        return self._state

    @property
    def current_track(self) -> Optional[QueueTrack]:
        """Get current track."""
        return self._current_track

    @property
    def duration_ms(self) -> int:
        """Get current track duration."""
        return self._current_duration_ms

    def get_state_dict(self) -> dict:
        """Get current state as dictionary for reporting."""
        track = self._current_track
        queue_item_id = track.queue_item_id if track else 0

        return {
            "playingState": int(self._state),
            "bufferState": int(BufferStatus.OK),
            "currentPosition": {
                "timestamp": self._position_timestamp_ms,
                "value": self._position_value_ms,
            },
            "duration": self._current_duration_ms,
            "currentQueueItemId": queue_item_id,
        }
