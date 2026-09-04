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
    from .play_reporter import PlayReporter

logger = logging.getLogger(__name__)

# Threshold for restart vs previous track (milliseconds)
PREVIOUS_TRACK_THRESHOLD_MS = 3000

# When we begin a track at a position beyond this, treat it as adopted mid-play
# from the controlling app (a Connect handoff) rather than a play we initiated.
# The app already reported/scrobbled that play, so we suppress our own
# reportStreamingStart to avoid a duplicate Last.fm scrobble.
_HANDOFF_POSITION_THRESHOLD_MS = 5000

# While paused, the monitor watches for an external renderer stop. A DLNA
# get_state() collapses transient read failures (and unrecognized device state
# strings) to STOPPED, so a real stop must be confirmed by this many consecutive
# STOPPED polls (~0.5s each) before ending the listen — one bad poll must not
# prematurely stop a normal paused track or lose its resume position.
_PAUSED_STOP_CONFIRMATIONS = 3

# After a WebSocket reconnect, the Qobuz server replays its last-known session
# snapshot via SET_STATE — typically PAUSED at a position from before the drop.
# If the renderer is actually still playing further along the same track, treat
# that as a stale replay and ignore the pause/seek. This is the minimum gap
# (renderer ahead of server) at which we suppress.
_STALE_SNAPSHOT_THRESHOLD_MS = 5000

# When the track we advance to cannot be played (Qobuz reports it "Not
# available" in the user's region), we report it as loading and wait for the
# server's SET_STATE naming the item after it. If that never arrives, stop
# rather than sit in LOADING forever.
_UNAVAILABLE_SKIP_TIMEOUT_S = 10.0


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
        play_reporter: Optional["PlayReporter"] = None,
    ):
        """Initialize player."""
        self.queue = queue
        self.metadata = metadata_service
        self.backend = backend
        # Optional: reports plays to Qobuz (listening history / Last.fm scrobbling).
        self._play_reporter = play_reporter

        # Current track
        self._current_track: Optional[QueueTrack] = None
        self._current_duration_ms: int = 0

        # Position tracking (timestamp-based like C++ implementation)
        self._position_timestamp_ms: int = 0
        self._position_value_ms: int = 0

        # State
        self._state: PlaybackState = PlaybackState.STOPPED

        # Consecutive STOPPED polls seen while paused (external-stop detection).
        self._paused_stop_polls = 0

        # Playback command serialization. A track switch in the Qobuz app sends
        # a burst of SET_STATE messages; without this lock the resulting
        # load/play/stop calls overlap and fire concurrent SOAP control
        # requests, which wedges DLNA AVTransport renderers. The generation
        # counter lets a newer command supersede an older one still waiting on
        # the lock (latest-command-wins).
        self._playback_lock = asyncio.Lock()
        self._command_generation = 0

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

        # Gapless playback state
        self._pending_next_track: Optional[dict] = None
        self._gapless_armed: bool = False
        self._transition_generation: int = 0
        self._gapless_arm_lock: asyncio.Lock = asyncio.Lock()

        # Unplayable-track skip: the track we are waiting to skip past, and the
        # timeout that gives up if the server never names its successor.
        self._skip_pending_track: Optional[QueueTrack] = None
        self._skip_timeout_task: Optional[asyncio.Task] = None

        # Callback for next track info changes (from command handler)
        self._on_next_track_changed_callback: Optional[Callable[[], None]] = None

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
        self.backend.on_next_track_started(self._on_next_track_started)

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
        self._clear_skip_pending()

        # Cancel background tasks
        for task in [self._playback_monitor_task, self._state_update_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close any open play report (incl. a paused listen, which no longer
        # closes on pause) so a shutdown mid-listen still lands in history.
        await self._report_stopped()

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

    async def broadcast_current_volume(self) -> None:
        """Refresh volume from the backend and re-emit it to the controller.

        Used when a controller (re)attaches — e.g. on `SrvrRndrSetActive(active=true)`
        — because the Qobuz cloud does not seem to replay our last
        `RndrSrvrVolumeChanged` to a freshly-subscribed controller, leaving the
        device picker without a volume bar until we send it again.
        """
        try:
            volume = await self.get_volume()
            await self._report_volume_change()
            logger.info(f"Re-broadcast current volume to app: {volume}%")
        except Exception as e:
            logger.warning(f"Failed to re-broadcast volume: {e}")

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
        if not self._current_track:
            logger.warning("Cannot seek: no track loaded")
            return False
        if self._state in (PlaybackState.STOPPED, PlaybackState.LOADING):
            # Nothing is playing on the backend yet; the caller applies the
            # start position when it issues play.
            logger.debug(f"Seek to {position_ms}ms ignored: track not started ({self._state.name})")
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

    def _next_generation(self) -> int:
        """Bump and return the playback command generation.

        Public command entrypoints call this before awaiting the playback
        lock. If a newer command bumps the generation while an older one is
        still queued on the lock, the older one detects the mismatch after
        acquiring and skips — so only the latest command in a burst runs.
        """
        self._command_generation += 1
        return self._command_generation

    async def apply_remote_state(
        self,
        *,
        track_id: Optional[str],
        queue_item_id: Optional[int],
        position_ms: Optional[int],
        playing_state: Optional[int],
        context_uuid: Optional[bytes] = None,
    ) -> None:
        """Apply a full SET_STATE intent from the app atomically.

        A SET_STATE is a multi-step intent (load this track, seek here, then
        play/pause/stop). Each SET_STATE message is dispatched as its own task,
        so if these steps were applied via separate locked methods they could
        interleave — an older SET_STATE could play a stale track after a newer
        one already queued a different load. Applying the whole sequence under a
        single lock acquisition and a single generation check makes the newest
        SET_STATE win as a unit, with no interleaving.

        Args:
            track_id: Target track id, or None if the message had no currentQueueItem.
            queue_item_id: Queue item id for the target track (if any).
            position_ms: Target position, or None if no currentPosition was sent.
            playing_state: Proto playing state (1=STOPPED, 2=PLAYING, 3=PAUSED),
                or None if the message had no playingState.
            context_uuid: Album/playlist context bytes for the target track, used
                for play reporting (listening history / scrobbles).
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("SET_STATE superseded by newer command; skipping")
                return

            # Detect a stale session-restore snapshot (server replays an old
            # PAUSED position after a reconnect while we're still playing). Done
            # before any mutation so a replayed snapshot can't overwrite live
            # state (position or context) with its outdated values.
            stale = self._is_stale_pause_snapshot_locked(track_id, position_ms, playing_state)

            if self._skip_pending_track is not None and track_id is None:
                logger.debug("Ignoring SET_STATE without a queue item while skipping a track")
                return

            # Load if a track is specified and differs from the loaded one.
            if track_id is not None:
                cur = self._current_track
                if cur is not None and cur is self._skip_pending_track and cur.track_id == track_id:
                    # The server acknowledged the unplayable item we reported as
                    # current; its nextQueueItem (already stored by the command
                    # handler) is where playback continues.
                    await self._skip_past_unplayable_locked()
                    return
                if cur is None or cur.track_id != track_id:
                    logger.info(f"Loading new track: {track_id}")
                    if not await self._load_track_locked(
                        queue_item_id or 0,
                        track_id,
                        context_uuid,
                        for_playback=playing_state == 2,
                    ):
                        failed = self._current_track
                        if (
                            playing_state == 2
                            and failed is not None
                            and failed.streaming_url is None
                        ):
                            # Asked to play a track that cannot be fetched (e.g.
                            # "Not available" in this region): skip it as the
                            # official app does.
                            await self._skip_past_unplayable_locked()
                        return
                    if gen != self._command_generation:
                        # A stop/next/other SET_STATE queued up while the URL
                        # and metadata were fetched (e.g. the server deactivated
                        # this renderer right after its join snapshot). Don't
                        # push this track to the backend only for the queued
                        # command to tear it down again.
                        logger.info(
                            f"SET_STATE superseded while loading track {track_id}; "
                            "not starting playback"
                        )
                        if self._state == PlaybackState.LOADING:
                            self._state = PlaybackState.STOPPED
                        return
                elif (
                    not stale
                    and queue_item_id
                    and cur.queue_item_id
                    and (cur.queue_item_id != queue_item_id)
                ):
                    # Same track but a different known queue occurrence id. Adopt
                    # it. Only split the play report when not currently playing:
                    # a paused/stopped track re-armed from a different slot is a
                    # distinct play, so end the prior report and let the
                    # subsequent play report fresh. While PLAYING the audio is
                    # continuous (e.g. a queue reorder reassigned the id), so it
                    # stays one listen — splitting it would double-scrobble.
                    cur.queue_item_id = queue_item_id
                    if context_uuid is not None:
                        cur.context_uuid = context_uuid
                    if self._state != PlaybackState.PLAYING:
                        await self._report_stopped()
                elif not stale:
                    # Same play. Fill in a late queue item id if we never had a
                    # real one, and adopt a changed/late context. Only overwrite
                    # context with a real value so a context-less SET_STATE can't
                    # wipe a known context.
                    if queue_item_id and not cur.queue_item_id:
                        cur.queue_item_id = queue_item_id
                    if context_uuid is not None and cur.context_uuid != context_uuid:
                        cur.context_uuid = context_uuid
                        # The play may already be active in the reporter (we
                        # return early from _play_locked while PLAYING), so
                        # re-sync its session or the end report keeps the old
                        # context.
                        if self._play_reporter:
                            self._play_reporter.update_context(
                                track_id=track_id,
                                context_uuid=self._format_context_uuid(context_uuid),
                            )

            # Position, then play/pause/stop — same order as the app expects.
            if position_ms is not None and not stale:
                await self.seek(position_ms)

            if playing_state is not None and not stale:
                # Proto: 1=STOPPED, 2=PLAYING, 3=PAUSED
                if playing_state == 2:
                    await self._play_locked(position_ms or 0)
                elif playing_state == 3:
                    await self._pause_locked()
                elif playing_state == 1:
                    await self._stop_playback_locked()

    def _is_stale_pause_snapshot_locked(
        self,
        track_id: Optional[str],
        position_ms: Optional[int],
        playing_state: Optional[int],
    ) -> bool:
        """Decide whether an inbound SET_STATE is a stale session-restore replay.

        Must be called while holding ``_playback_lock`` so the live player state
        it reads is consistent with the surrounding mutation. Returns True when
        ALL of:
          - server says PAUSED
          - renderer is still PLAYING
          - it's the same track the renderer is on
          - server position is more than _STALE_SNAPSHOT_THRESHOLD_MS behind the
            renderer's actual position
        """
        if playing_state != 3:
            return False
        if self._state != PlaybackState.PLAYING:
            return False
        if position_ms is None:
            return False
        # A different target track is a real command (track change), not a replay.
        cur = self._current_track
        if track_id is not None and (cur is None or cur.track_id != track_id):
            return False

        actual_pos = self.current_position_ms
        gap_ms = actual_pos - position_ms
        if gap_ms <= _STALE_SNAPSHOT_THRESHOLD_MS:
            return False

        logger.info(
            "Ignoring stale SET_STATE: server says PAUSED at %dms, renderer is "
            "PLAYING at %dms (%.1fs ahead) on same track — likely a session-"
            "restore replay after WebSocket reconnect; keeping playback.",
            position_ms,
            actual_pos,
            gap_ms / 1000.0,
        )
        return True

    async def play(self, position_ms: int = 0) -> bool:
        """
        Start or resume playback.

        Args:
            position_ms: Optional starting position (only used when starting new playback)

        Returns:
            True if playback started/resumed successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("play superseded by newer command; skipping")
                return False
            return await self._play_locked(position_ms)

    async def _play_locked(self, position_ms: int = 0) -> bool:
        logger.debug(f"Play command, current state: {self._state}")

        # Resume from pause
        if self._state == PlaybackState.PAUSED:
            if not await self.backend.resume():
                # The renderer rejected the resume (e.g. SOAP failure) — stay
                # PAUSED rather than reporting PLAYING over a silent device, and
                # push the real PAUSED state so the app (which requested PLAY)
                # corrects immediately instead of waiting for the next heartbeat.
                logger.warning("Backend failed to resume; remaining paused")
                await self._send_state_update()
                return False
            self._state = PlaybackState.PLAYING
            self._position_timestamp_ms = int(time.time() * 1000)
            await self._send_state_update()
            # Resume continues an existing listen — pass the current position so
            # we don't re-report a start (and re-scrobble) on every pause/resume.
            await self._report_playing(self._position_value_ms)
            logger.info("Playback resumed")
            return True

        # Already playing — seek if position changed
        if self._state == PlaybackState.PLAYING:
            if position_ms > 0:
                logger.info(f"Scrubbing to {position_ms}ms while playing")
                await self.seek(position_ms)
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
        success = await self._start_playback(position_ms)

        # Seek if position > 0 and playback started
        if success and position_ms > 0:
            await self.backend.seek(position_ms)

        return success

    async def reload_current_track(self) -> bool:
        """
        Reload the current track (e.g. after quality change).

        Saves position, stops, clears cached URL, and restarts at saved position.

        Returns:
            True if track was reloaded successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("reload_current_track superseded by newer command; skipping")
                return False
            return await self._reload_current_track_locked()

    async def _reload_current_track_locked(self) -> bool:
        if not self._current_track:
            return False

        if self._state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # Not actively playing — just clear cached URL so next play uses new quality
            self._current_track.streaming_url = None
            return True

        was_playing = self._state == PlaybackState.PLAYING

        # Save current position
        saved_position = self.current_position_ms
        logger.info(
            f"Reloading track {self._current_track.track_id} at position {saved_position}ms"
        )

        # Stop current playback
        await self.backend.stop()

        # Clear cached streaming URL so it's re-fetched at new quality
        self._current_track.streaming_url = None

        if was_playing:
            # Restart playback from saved position
            success = await self._start_playback(saved_position)
            if success and saved_position > 0:
                await self.backend.seek(saved_position)
            return success
        else:
            # Was paused — just reset state, will re-fetch URL on next play
            self._state = PlaybackState.STOPPED
            self._position_value_ms = saved_position
            self._position_timestamp_ms = int(time.time() * 1000)
            # End the paused play's report: the next play re-fetches at the new
            # quality (new blob/format), so it must report as a fresh play rather
            # than resume this now-stale session.
            await self._report_stopped()
            return True

    async def pause(self) -> bool:
        """
        Pause playback.

        Returns:
            True if paused successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("pause superseded by newer command; skipping")
                return False
            return await self._pause_locked()

    async def _pause_locked(self) -> bool:
        if self._state != PlaybackState.PLAYING:
            logger.debug(f"Cannot pause in state {self._state}")
            return False

        # Capture position before pausing
        self._position_value_ms = self.current_position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        await self.backend.pause()
        self._state = PlaybackState.PAUSED
        await self._send_state_update()
        # A pause does not end the listen — keeping the play-reporting session
        # open across pause/resume avoids emitting a streaming-end (and a
        # duplicate scrobble) on every pause. The session is closed on a real
        # stop, track change, or track end. note_paused stops the played-time
        # clock so paused time is excluded from the reported duration.
        self._report_paused()

        logger.info("Playback paused")
        return True

    async def stop_playback(self) -> None:
        """
        Stop playback completely.

        Resets position to 0 but keeps queue position.
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("stop_playback superseded by newer command; skipping")
                return
            await self._stop_playback_locked()

    async def _stop_playback_locked(self) -> None:
        # Clear gapless state — explicit stop
        self._clear_gapless_state()
        self._clear_skip_pending()

        await self.backend.stop()

        self._state = PlaybackState.STOPPED
        self._position_value_ms = 0
        self._position_timestamp_ms = int(time.time() * 1000)

        await self._send_state_update()
        await self._report_stopped()
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
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("load_track superseded by newer command; skipping")
                return False
            return await self._load_track_locked(queue_item_id, track_id)

    async def _load_track_locked(
        self,
        queue_item_id: int,
        track_id: str,
        context_uuid: Optional[bytes] = None,
        *,
        for_playback: bool = False,
    ) -> bool:
        """Replace the current track and pre-fetch its URL and metadata.

        ``for_playback`` says the caller starts playback right after. The player
        then sits in LOADING rather than STOPPED for the load. LOADING is
        reported as PLAYING + BUFFERING, so a track change never reaches the
        app as "the renderer stopped" — which the app answered with a PAUSED
        SET_STATE, leaving a manual skip on a paused track (GitHub #22).
        """
        logger.info(f"Loading track: track_id={track_id}, queue_item_id={queue_item_id}")
        self._clear_skip_pending()

        # Stop current playback if playing
        if self._state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.backend.stop()
            # End the outgoing track's play report now that it's being replaced.
            # Pause no longer ends the session, so a load-only track change (no
            # immediate play) would otherwise leave the previous play unreported.
            await self._report_stopped()
        self._state = PlaybackState.LOADING if for_playback else PlaybackState.STOPPED

        # Create track object. The context UUID identifies the album/playlist the
        # track is played from and is required for Qobuz listening history /
        # Last.fm scrobbles, so it must be carried onto the QueueTrack.
        self._current_track = QueueTrack(
            queue_item_id=queue_item_id,
            track_id=track_id,
            context_uuid=context_uuid,
        )
        # A fresh track starts at 0 (callers starting elsewhere set the position
        # after loading) and its duration is unknown until metadata arrives.
        # Without this a report sent during the load shows the new item at the
        # old track's position and length.
        self._set_position(0)
        self._current_duration_ms = 0

        # Pre-fetch URL and metadata
        try:
            url = await self._get_track_url(track_id)
            if url:
                self._current_track.set_streaming_url(url)
            else:
                logger.error(f"Failed to get URL for track {track_id}")
                self._state = PlaybackState.STOPPED
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
            self._state = PlaybackState.STOPPED
            return False

    async def play_track(
        self,
        queue_item_id: int,
        track_id: str,
        position_ms: int = 0,
        context_uuid: Optional[bytes] = None,
    ) -> bool:
        """
        Play a specific track from the queue.

        Args:
            queue_item_id: Queue item identifier
            track_id: Qobuz track ID
            position_ms: Starting position in milliseconds
            context_uuid: Album/playlist context bytes, used for play reporting.

        Returns:
            True if playback started successfully
        """
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("play_track superseded by newer command; skipping")
                return False
            return await self._play_track_locked(queue_item_id, track_id, position_ms, context_uuid)

    async def _play_track_locked(
        self,
        queue_item_id: int,
        track_id: str,
        position_ms: int = 0,
        context_uuid: Optional[bytes] = None,
    ) -> bool:
        # Clear gapless state — explicit track change
        self._clear_gapless_state()

        logger.info(
            f"Play track requested: track_id={track_id}, queue_item_id={queue_item_id}, pos={position_ms}ms"
        )

        # Load the track first
        if not await self._load_track_locked(
            queue_item_id, track_id, context_uuid, for_playback=True
        ):
            return False

        # Set starting position
        self._position_value_ms = position_ms
        self._position_timestamp_ms = int(time.time() * 1000)

        # Start playback
        success = await self._start_playback(position_ms)

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
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("next_track superseded by newer command; skipping")
                return False
            return await self._next_track_locked()

    async def _next_track_locked(self) -> bool:
        # Clear gapless state — explicit skip
        self._clear_gapless_state()
        self._clear_skip_pending()

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
            # Report the finished play so the last track lands in listening
            # history / is scrobbled, and the lingering session is closed (an
            # open session would inflate the next play's reported duration).
            await self._report_stopped()
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
        gen = self._next_generation()
        async with self._playback_lock:
            if gen != self._command_generation:
                logger.debug("previous_track superseded by newer command; skipping")
                return False
            return await self._previous_track_locked()

    async def _previous_track_locked(self) -> bool:
        # Clear gapless state — explicit navigation
        self._clear_gapless_state()
        self._clear_skip_pending()

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
            if self._state == PlaybackState.PAUSED:
                # Restarting a paused track ends the prior listen so the next
                # resume reports the replay as a fresh play instead of merging
                # into the open (paused) session.
                await self._report_stopped()
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

    async def _start_playback(self, start_position_ms: int = 0) -> bool:
        """
        Start playback of current track.

        Args:
            start_position_ms: Position the track begins at. A large value means
                we're adopting an in-progress track from the app (handoff), which
                suppresses our play-start report.

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
            # Get streaming URL if not cached. A cached URL past its TTL is
            # treated as absent: a track loaded PAUSED and played later than
            # the URL lifetime must not start from an expired URL.
            url = None if track.url_is_stale() else track.streaming_url
            if not url:
                url = await self._get_track_url(track.track_id)
                if not url:
                    logger.error(f"Failed to get URL for track {track.track_id}")
                    self._state = PlaybackState.ERROR
                    await self._send_state_update()
                    return False
                track.set_streaming_url(url)

            # Get metadata if not cached
            meta: Optional[dict] = track.metadata if track.metadata else None
            if not meta:
                meta = await self._get_track_metadata(track.track_id)
                if meta:
                    track.metadata = meta
                    track.duration_ms = meta.get("duration_ms", 0)

            # Get actual quality and format info from cache (set during URL fetch)
            actual_quality, sample_rate, bit_depth = self.metadata.get_track_format(track.track_id)

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
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            )

            # Log now playing with actual quality (0 = cache miss, fall back to max quality)
            self.metadata.log_now_playing_info(backend_meta, actual_quality or None)

            # Report file quality if callback is set
            if self._file_quality_report_callback:
                logger.debug(f"Track {track.track_id} actual_quality={actual_quality}")
                if actual_quality:
                    await self._file_quality_report_callback(actual_quality)
                else:
                    logger.debug(
                        f"No actual_quality for track {track.track_id}, skipping file quality report"
                    )

            # Start playback on backend
            await self.backend.play(url, backend_meta)

            # Update state. Report the start position, not 0 — the caller
            # seeks the backend right after, and reporting 0 first makes the
            # app's progress bar snap to 0:00 until the next heartbeat.
            self._state = PlaybackState.PLAYING
            self._current_duration_ms = track.duration_ms
            self._position_value_ms = start_position_ms
            self._position_timestamp_ms = int(time.time() * 1000)

            await self._send_state_update()
            await self._report_playing(start_position_ms)
            return True

        except Exception as e:
            logger.error(f"Failed to start playback: {e}", exc_info=True)
            self._state = PlaybackState.ERROR
            await self._send_state_update()
            return False

    # =========================================================================
    # Play Reporting (Qobuz listening history / Last.fm scrobbling)
    # =========================================================================

    async def _report_playing(self, start_position_ms: int = 0) -> None:
        """Tell the play reporter the current track is now playing.

        A start beyond the handoff threshold means the app already owns (and
        scrobbled) this play, so we track it locally but suppress our start
        report to avoid a duplicate scrobble.
        """
        if not self._play_reporter or not self._current_track:
            return
        track = self._current_track
        format_id = self.metadata.get_track_actual_quality(track.track_id) or 0
        blob = self.metadata.get_track_blob(track.track_id) or ""
        report_start = start_position_ms < _HANDOFF_POSITION_THRESHOLD_MS
        await self._play_reporter.note_playing(
            track_id=track.track_id,
            format_id=format_id,
            blob=blob,
            context_uuid=self._format_context_uuid(track.context_uuid),
            report_start=report_start,
        )

    async def _report_stopped(self) -> None:
        """Tell the play reporter playback stopped (pause/stop/track end)."""
        if not self._play_reporter:
            return
        await self._play_reporter.note_stopped()

    def _report_paused(self) -> None:
        """Tell the play reporter playback paused (session stays open)."""
        if self._play_reporter:
            self._play_reporter.note_paused()

    @staticmethod
    def _format_context_uuid(context_uuid: Optional[bytes]) -> Optional[str]:
        """Format the 16-byte queue context UUID as a canonical UUID string."""
        if not context_uuid:
            return None
        try:
            import uuid

            return str(uuid.UUID(bytes=bytes(context_uuid)))
        except (ValueError, TypeError):
            return None

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
        # Snapshot the track that ended synchronously, before any queued user
        # command (stop/next/play) task can run. The automatic repeat restart
        # is only valid while this exact track is still the active one.
        asyncio.create_task(self._handle_track_ended(self._current_track))

    async def _handle_track_ended(self, ended_track: Optional[QueueTrack]) -> None:
        """Handle natural track end.

        ``ended_track`` is the track that was playing when the backend reported
        the end, used to detect a user command that superseded the restart.
        """
        # Clear gapless state — prevents stale gapless callbacks from racing
        self._transition_generation += 1
        self._gapless_armed = False
        self._pending_next_track = None

        logger.info("Track ended naturally")

        # The track finished — report the completed play before advancing.
        await self._report_stopped()

        # Get queue state to check repeat mode
        queue_state = await self.queue.get_state()

        if queue_state.repeat_mode == RepeatMode.ONE and ended_track is not None:
            # Restart the current track from the beginning under repeat-one,
            # unless a user command superseded us while we were reporting.
            await self._restart_current_track(ended_track)
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
                started = await self.play_track(
                    queue_item_id=next_track_info["queueItemId"],
                    track_id=next_track_info["trackId"],
                    position_ms=0,
                    context_uuid=next_track_info.get("contextUuid"),
                )
                if not started:
                    # e.g. "Not available" in this region — skip past it
                    # instead of leaving the album stopped (GitHub #21).
                    await self._skip_past_unplayable(next_track_info)
                return

        # No next track available - stop playback
        logger.info("No next track available - playback stopped")
        self._state = PlaybackState.STOPPED
        self._current_track = None
        self._position_value_ms = 0
        await self._send_state_update()

    async def _restart_current_track(self, ended_track: QueueTrack) -> None:
        """Restart the current track from the beginning (repeat-one).

        ``ended_track`` is the track that ended. The restart only proceeds while
        that exact track is still active and the player is still PLAYING — a
        user stop (-> STOPPED), pause (-> PAUSED), or next/play (different
        ``_current_track``) that raced the natural end therefore wins, and we
        skip the replay rather than override their intent.
        """
        async with self._playback_lock:
            if self._current_track is not ended_track or self._state != PlaybackState.PLAYING:
                logger.debug("repeat-one restart superseded by user command; skipping")
                return
            await self._restart_current_track_locked()

    async def _restart_current_track_locked(self) -> None:
        """Restart the current track, assuming the playback lock is held.

        On natural end the backend has already transitioned to STOPPED, so a
        bare seek(0) leaves it silent — we must re-issue play. The cached URL
        is cleared so a fresh, non-expired streaming link is fetched for the
        repeat. ``_start_playback`` reports the new play; the completed one was
        already reported by the caller.
        """
        if not self._current_track:
            return
        self._current_track.streaming_url = None
        self._set_position(0)
        await self._start_playback()

    # =========================================================================
    # Unplayable Track Skipping
    # =========================================================================
    #
    # Qobuz albums can contain tracks that are "Not available" in the user's
    # region: track/get answers 404 and no streaming URL exists. The official
    # app skips them; so must we, or an album stops dead at the first one.
    #
    # A renderer only learns the current and the next queue item (via
    # SET_STATE), so once the next item turns out to be unplayable we don't know
    # what follows it. The server does send a SET_STATE with a fresh
    # nextQueueItem after we report a new current item, so: adopt the unplayable
    # item as current, report it as loading (PLAYING + BUFFERING on the wire),
    # and let apply_remote_state() finish the skip when that SET_STATE arrives.
    # A timeout stops playback if the server never answers.

    def _clear_skip_pending(self) -> None:
        """Forget a pending unplayable-track skip and cancel its timeout."""
        self._skip_pending_track = None
        task = self._skip_timeout_task
        self._skip_timeout_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _begin_skip_wait_locked(self) -> None:
        """Report the unplayable current track and wait for the server to name its successor."""
        track = self._current_track
        if track is None:
            return
        self._clear_skip_pending()
        self._skip_pending_track = track
        self._state = PlaybackState.LOADING
        self._current_duration_ms = 0
        self._set_position(0)
        logger.info(
            f"Track {track.track_id} cannot be played (not available); "
            "waiting for the next queue item to skip to"
        )
        self._skip_timeout_task = asyncio.create_task(self._skip_wait_timeout(track))
        await self._send_state_update()

    async def _skip_wait_timeout(self, pending: QueueTrack) -> None:
        await asyncio.sleep(_UNAVAILABLE_SKIP_TIMEOUT_S)
        async with self._playback_lock:
            if self._skip_pending_track is not pending:
                return
            logger.warning(
                f"No next track received after unplayable track {pending.track_id}; "
                "stopping playback"
            )
            self._clear_skip_pending()
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._set_position(0)
            await self._send_state_update()

    async def _skip_past_unplayable_locked(self) -> bool:
        """Skip from the unplayable current track to the server-provided next item.

        Returns True if a playable track was started. If the next item is
        unplayable too, the wait starts over for the item after it; if there is
        no next item, playback stops at the end of the queue.
        """
        self._clear_skip_pending()
        next_info = self._get_next_track_callback() if self._get_next_track_callback else None
        if not next_info:
            logger.info("No playable track follows the unavailable one; playback stopped")
            self._state = PlaybackState.STOPPED
            self._current_track = None
            self._set_position(0)
            await self._send_state_update()
            return False

        # Consume the next item like auto-advance does, or the gapless arm would
        # queue the track we are about to play as its own successor.
        if self._clear_next_track_callback:
            self._clear_next_track_callback()

        logger.info(f"Skipping unavailable track; playing next item {next_info['trackId']}")
        if await self._play_track_locked(
            next_info["queueItemId"],
            next_info["trackId"],
            0,
            next_info.get("contextUuid"),
        ):
            return True
        failed = self._current_track
        if failed is not None and failed.streaming_url is None:
            await self._begin_skip_wait_locked()
        return False

    async def _skip_past_unplayable(self, failed_info: dict) -> None:
        """Start the skip flow after an auto-advance into an unplayable track."""
        async with self._playback_lock:
            cur = self._current_track
            if (
                cur is None
                or cur.track_id != failed_info["trackId"]
                or cur.queue_item_id != failed_info["queueItemId"]
                or cur.streaming_url is not None
            ):
                # A user command took over meanwhile, or the failure was in the
                # backend rather than the track being unavailable.
                return
            await self._begin_skip_wait_locked()

    def _on_playback_error(self, message: str) -> None:
        """Callback when backend reports playback error."""
        logger.error(f"Playback error: {message}")
        self._state = PlaybackState.ERROR
        asyncio.create_task(self._send_state_update())

    def _on_position_update(self, position_ms: int) -> None:
        """Callback when backend reports position update."""
        self._set_position(position_ms)

    # =========================================================================
    # Gapless Playback
    # =========================================================================

    def _clear_gapless_state(self) -> None:
        """Clear all gapless state and increment generation."""
        self._transition_generation += 1
        self._gapless_armed = False
        self._pending_next_track = None

    async def _prepare_next_track_for_gapless(self) -> None:
        """Prepare the next track for gapless playback on the backend.

        Serialized via a lock: overlapping calls (monitor loop racing a
        re-arm) would each push the next track to the backend, and on Sonos
        that queues the track twice — making it play twice.
        """
        if not self.backend.supports_gapless or self._gapless_armed:
            return

        if not self._get_next_track_callback:
            return

        async with self._gapless_arm_lock:
            await self._prepare_next_track_locked()

    async def _prepare_next_track_locked(self) -> None:
        """Arm the next track. Caller must hold `_gapless_arm_lock`."""
        # Re-check after waiting on the lock — a concurrent arm may have won
        if self._gapless_armed or not self._get_next_track_callback:
            return
        # Only a playing track has a "next". Arming after a stop landed (e.g.
        # a deactivation racing a just-started play) would queue a track on an
        # idle renderer.
        if self._state != PlaybackState.PLAYING:
            return

        next_track_info = self._get_next_track_callback()
        if not next_track_info:
            return

        track_id = next_track_info["trackId"]
        queue_item_id = next_track_info["queueItemId"]
        my_generation = self._transition_generation

        try:
            # Fetch URL and metadata
            url = await self._get_track_url(track_id)
            if not url:
                logger.debug(f"Gapless: failed to get URL for next track {track_id}")
                return

            meta = await self._get_track_metadata(track_id)

            _, sample_rate, bit_depth = self.metadata.get_track_format(track_id)
            backend_meta = BackendTrackMetadata(
                track_id=track_id,
                title=meta.get("title", f"Track {track_id}") if meta else f"Track {track_id}",
                artist=meta.get("artist", "") if meta else "",
                album=meta.get("album", "") if meta else "",
                duration_ms=meta.get("duration_ms", 0) if meta else 0,
                artwork_url=meta.get("artwork_url", "") if meta else "",
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            )

            success = await self.backend.set_next_track(url, backend_meta, queue_item_id)

            # State changed while arming (skip, stop, queue edit) — the arm
            # is stale; undo it on the backend instead of marking it armed
            if my_generation != self._transition_generation:
                logger.debug(f"Gapless: discarding stale arm for track {track_id}")
                if success:
                    await self.backend.clear_next_track()
                return

            if success:
                self._pending_next_track = {
                    "trackId": track_id,
                    "queueItemId": queue_item_id,
                    "contextUuid": next_track_info.get("contextUuid"),
                    "url": url,
                    "metadata": meta,
                    "backend_meta": backend_meta,
                }
                self._gapless_armed = True
                logger.info(f"Gapless: armed next track {track_id}")
            else:
                logger.debug(f"Gapless: backend rejected next track {track_id}")

        except Exception as e:
            logger.warning(f"Gapless: failed to prepare next track: {e}")

    def _on_next_track_started(self) -> None:
        """Callback when backend reports gapless transition to next track."""
        logger.debug("Gapless: next track started callback from backend")
        asyncio.create_task(self._handle_gapless_transition())

    async def _handle_gapless_transition(self) -> None:
        """Handle a gapless transition to the next track."""
        # Capture generation to detect concurrent state changes (e.g. explicit skip/stop)
        my_generation = self._transition_generation

        if not self._pending_next_track or not self._gapless_armed:
            logger.warning("Gapless: transition callback but no pending track")
            return

        # Check generation hasn't changed (guards against concurrent transitions)
        if my_generation != self._transition_generation:
            logger.debug("Gapless: stale transition callback, ignoring")
            return

        next_info = self._pending_next_track
        track_id = next_info["trackId"]
        queue_item_id = next_info["queueItemId"]
        meta = next_info.get("metadata")

        logger.info(f"Gapless: transitioning to track {track_id}")

        # Update current track (no stop/start cycle)
        self._current_track = QueueTrack(
            queue_item_id=queue_item_id,
            track_id=track_id,
            context_uuid=next_info.get("contextUuid"),
            streaming_url=next_info.get("url"),
            metadata=meta or {},
            duration_ms=meta.get("duration_ms", 0) if meta else 0,
        )
        self._current_duration_ms = self._current_track.duration_ms

        # Reset position
        self._position_value_ms = 0
        self._position_timestamp_ms = int(time.time() * 1000)

        # Clear gapless state
        self._pending_next_track = None
        self._gapless_armed = False

        # Clear next track info from command handler
        if self._clear_next_track_callback:
            self._clear_next_track_callback()

        # Report file quality
        actual_quality = self.metadata.get_track_actual_quality(track_id)
        backend_meta = next_info.get("backend_meta")
        if backend_meta:
            self.metadata.log_now_playing_info(backend_meta, actual_quality)
        if self._file_quality_report_callback and actual_quality:
            await self._file_quality_report_callback(actual_quality)

        # Report the play swap: ends the previous track, starts this one.
        await self._report_playing()

        # Send state update
        await self._send_state_update()

        # Try to arm the next next track
        await self._prepare_next_track_for_gapless()

    async def on_next_track_info_changed(self) -> None:
        """Called when command handler reports the next track info has changed."""
        new_info = self._get_next_track_callback() if self._get_next_track_callback else None

        # The server resends the same next track in bursts — if it's already
        # armed, re-arming would queue a duplicate on the backend
        pending = self._pending_next_track
        if (
            self._gapless_armed
            and new_info is not None
            and pending is not None
            and new_info["trackId"] == pending["trackId"]
            and new_info["queueItemId"] == pending["queueItemId"]
            and new_info.get("contextUuid") == pending.get("contextUuid")
        ):
            logger.debug("Gapless: next track unchanged, keeping current arming")
            return

        logger.debug("Gapless: next track info changed, re-arming")

        async with self._gapless_arm_lock:
            # Clear current gapless arming
            self._transition_generation += 1
            self._gapless_armed = False
            self._pending_next_track = None
            await self.backend.clear_next_track()

            # Re-arm with new track if playing
            if self._state == PlaybackState.PLAYING:
                await self._prepare_next_track_locked()

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _playback_monitor_loop(self) -> None:
        """Monitor playback and handle backend state changes."""
        while self._is_running:
            try:
                await asyncio.sleep(0.5)

                if self._state == PlaybackState.PLAYING:
                    self._paused_stop_polls = 0
                    # Poll backend state
                    backend_state = await self.backend.get_state()

                    if backend_state == PlaybackState.STOPPED:
                        # Track finished naturally (handled by callback)
                        pass
                    elif backend_state == PlaybackState.PAUSED:
                        # External pause (e.g., DLNA device)
                        self._state = PlaybackState.PAUSED
                        await self._send_state_update()
                        # Stop the played-time clock so this pause is excluded
                        # from the reported duration, like an app-driven pause.
                        self._report_paused()

                    # Update position from backend
                    position = await self.backend.get_position()
                    self._set_position(position)

                    # Try to arm gapless if not already armed
                    if not self._gapless_armed:
                        await self._prepare_next_track_for_gapless()

                elif self._state == PlaybackState.PAUSED:
                    # Keep watching while paused: a pause leaves the play-report
                    # session open, so an external stop/timeout on the renderer
                    # must close it (otherwise a later play merges into it).
                    # Require consecutive STOPPED polls before trusting it —
                    # get_state() reports STOPPED on a transient read failure, and
                    # one bad poll must not end a normal paused listen.
                    if await self.backend.get_state() == PlaybackState.STOPPED:
                        self._paused_stop_polls += 1
                        if self._paused_stop_polls >= _PAUSED_STOP_CONFIRMATIONS:
                            self._paused_stop_polls = 0
                            self._state = PlaybackState.STOPPED
                            # Zero the position like _stop_playback_locked: a
                            # stale pause-point position makes "previous" try a
                            # restart-seek on a stopped renderer (a no-op)
                            # instead of navigating.
                            self._position_value_ms = 0
                            self._position_timestamp_ms = int(time.time() * 1000)
                            await self._send_state_update()
                            await self._report_stopped()
                    else:
                        self._paused_stop_polls = 0

                else:
                    self._paused_stop_polls = 0

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
