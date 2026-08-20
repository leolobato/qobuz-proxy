"""
Playback command handler for WebSocket integration.

Processes playback commands from the Qobuz app via WsManager.
"""

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from .player import QobuzPlayer
    from .queue import QobuzQueue

logger = logging.getLogger(__name__)

# Quality change callback type
QualityChangeCallback = Callable[[int], Awaitable[None]]

# QConnect message types for Server -> Renderer commands
MSG_TYPE_SET_STATE = 41  # SrvrRndrSetState: play/pause/stop, position, queue item
MSG_TYPE_SET_ACTIVE = 43  # SrvrRndrSetActive: renderer activation state
MSG_TYPE_SET_MAX_AUDIO_QUALITY = 44  # SrvrRndrSetMaxAudioQuality
MSG_TYPE_SET_LOOP_MODE = 45  # SrvrRndrSetLoopMode
MSG_TYPE_SET_SHUFFLE_MODE = 46  # SrvrRndrSetShuffleMode
MSG_TYPE_SET_AUTOPLAY_MODE = 47  # SrvrRndrSetAutoplayMode

# The app sends nextQueueItem with all-ones ids to signal "no next track"
# (e.g. on the last track of an album when the next album lives in a different
# queue context). trackId is a fixed32, queueItemId a uint64 on the wire.
SENTINEL_TRACK_ID = 0xFFFFFFFF
SENTINEL_QUEUE_ITEM_ID = 0xFFFFFFFFFFFFFFFF


class PlaybackCommandHandler:
    """
    Handles playback commands from WebSocket.

    Translates protobuf messages to player operations.
    """

    def __init__(
        self,
        player: "QobuzPlayer",
        queue: Optional["QobuzQueue"] = None,
        on_quality_change: Optional[QualityChangeCallback] = None,
    ):
        """
        Initialize command handler.

        Args:
            player: QobuzPlayer instance
            queue: Optional QobuzQueue (defaults to player.queue)
            on_quality_change: Optional callback for quality change events
        """
        self.player = player
        self.queue = queue or player.queue
        self._on_quality_change = on_quality_change

        # Store next track info for auto-advance (from SET_STATE nextQueueItem)
        self._next_track_info: Optional[dict] = None

        # Callback when next track info changes (for gapless re-arming)
        self._on_next_track_changed: Optional[Callable[[], Awaitable[None]]] = None

    def get_message_types(self) -> list[int]:
        """Get list of message types this handler processes."""
        return [
            MSG_TYPE_SET_STATE,
            MSG_TYPE_SET_ACTIVE,
            MSG_TYPE_SET_MAX_AUDIO_QUALITY,
            MSG_TYPE_SET_LOOP_MODE,
            MSG_TYPE_SET_SHUFFLE_MODE,
            MSG_TYPE_SET_AUTOPLAY_MODE,
        ]

    async def handle_message(self, msg_type: int, message: Any) -> None:
        """Handle a playback command message."""
        try:
            if msg_type == MSG_TYPE_SET_STATE:
                await self._handle_set_state(message)
            elif msg_type == MSG_TYPE_SET_ACTIVE:
                await self._handle_set_active(message)
            elif msg_type == MSG_TYPE_SET_MAX_AUDIO_QUALITY:
                await self._handle_set_max_audio_quality(message)
            elif msg_type == MSG_TYPE_SET_LOOP_MODE:
                await self._handle_set_loop_mode(message)
            elif msg_type == MSG_TYPE_SET_SHUFFLE_MODE:
                await self._handle_set_shuffle_mode(message)
            elif msg_type == MSG_TYPE_SET_AUTOPLAY_MODE:
                await self._handle_set_autoplay_mode(message)
            else:
                logger.warning(f"Unhandled playback message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error handling playback command {msg_type}: {e}", exc_info=True)

    async def _handle_set_state(self, message: Any) -> None:
        """
        Handle SET_STATE message (type 41).

        This is the main playback control message from the server.
        Contains: playingState, currentPosition, queueVersion, currentQueueItem, nextQueueItem

        Important: Track info must be loaded BEFORE applying playingState, because
        the app may send track info with PAUSED state first, then PLAYING later.

        Note: For renderers, the server sends track info via SET_STATE rather than
        queue state messages (types 90/91). We store the next track info for
        auto-advance when the current track ends.
        """
        if not message.HasField("srvrRndrSetState"):
            logger.warning("SET_STATE message missing srvrRndrSetState field")
            return

        state = message.srvrRndrSetState
        logger.debug(f"SET_STATE received: {state}")

        # Extract current queue item info
        current_item = None
        current_queue_item_id = None
        current_track_id = None
        current_context_uuid = None
        if state.HasField("currentQueueItem"):
            current_item = state.currentQueueItem
            current_queue_item_id = current_item.queueItemId
            current_track_id = current_item.trackId
            current_context_uuid = current_item.contextUuid if current_item.contextUuid else None
            logger.debug(
                f"Current queue item: queueItemId={current_queue_item_id}, trackId={current_track_id}"
            )

        # Extract and store next queue item for auto-advance
        next_track_changed = False
        next_item = state.nextQueueItem if state.HasField("nextQueueItem") else None
        if next_item is not None and (
            next_item.trackId == SENTINEL_TRACK_ID
            or next_item.queueItemId == SENTINEL_QUEUE_ITEM_ID
        ):
            # "No next track" sentinel — treat like an absent nextQueueItem,
            # otherwise auto-advance tries to load track 4294967295 and fails.
            logger.debug("Next track sentinel received (no next track)")
            next_item = None
        if next_item is not None:
            old_queue_item_id = (
                self._next_track_info.get("queueItemId") if self._next_track_info else None
            )
            old_context_uuid = (
                self._next_track_info.get("contextUuid") if self._next_track_info else None
            )
            new_context_uuid = next_item.contextUuid if next_item.contextUuid else None
            # Preserve a previously-known context if the server resends the same
            # next item without the optional contextUuid, so we don't lose it for
            # the next play (mirrors the current-track behaviour).
            if new_context_uuid is None and next_item.queueItemId == old_queue_item_id:
                new_context_uuid = old_context_uuid
            new_next_info = {
                "queueItemId": next_item.queueItemId,
                "trackId": str(next_item.trackId),
                "contextUuid": new_context_uuid,
            }
            # Detect change by queueItemId (handles same track at different
            # positions) or by a changed/late-arriving context UUID, which the
            # gapless arm must pick up so the next play reports the right context.
            if (
                new_next_info["queueItemId"] != old_queue_item_id
                or new_next_info["contextUuid"] != old_context_uuid
            ):
                next_track_changed = True
            self._next_track_info = new_next_info
            logger.debug(
                f"Next track stored: queueItemId={next_item.queueItemId}, trackId={next_item.trackId}"
            )
        elif self._next_track_info is not None:
            # nextQueueItem disappeared — clear and notify
            self._next_track_info = None
            next_track_changed = True
            logger.debug("Next track cleared (nextQueueItem not present in SET_STATE)")

        # Keep the queue's current index in sync with the item the app shows.
        # Without this, queue-based fallbacks (auto-advance at track end,
        # get_current_track) act on a stale index — e.g. restarting the same
        # track instead of advancing.
        if current_queue_item_id is not None:
            await self.queue.set_current_by_item_id(current_queue_item_id)

        # Apply the desired remote state as a single atomic unit. A SET_STATE is
        # a multi-step intent (load this track, seek here, then play/pause/stop)
        # and each SET_STATE message runs in its own task, so applying the steps
        # via separate locked player calls could interleave and leave playback on
        # a stale track. apply_remote_state() applies the whole sequence under one
        # lock acquisition + generation check (newest SET_STATE wins as a unit),
        # and also handles the stale session-restore snapshot detection.
        await self.player.apply_remote_state(
            track_id=str(current_track_id) if current_item else None,
            queue_item_id=current_queue_item_id,
            position_ms=state.currentPosition if state.HasField("currentPosition") else None,
            playing_state=state.playingState if state.HasField("playingState") else None,
            context_uuid=current_context_uuid,
        )

        # Notify gapless system about next track change (after state handling)
        if next_track_changed and self._on_next_track_changed:
            await self._on_next_track_changed()

    def get_next_track_info(self) -> Optional[dict]:
        """Get the stored next track info for auto-advance."""
        return self._next_track_info

    def clear_next_track_info(self) -> None:
        """Clear the stored next track info after it's been used."""
        self._next_track_info = None

    def set_on_next_track_changed(self, callback: Optional[Callable[[], Awaitable[None]]]) -> None:
        """Set callback for when next track info changes (for gapless re-arming)."""
        self._on_next_track_changed = callback

    async def _handle_set_active(self, message: Any) -> None:
        """
        Handle SET_ACTIVE message (type 43).

        This tells the renderer if it's the currently active playback device.
        """
        if not message.HasField("srvrRndrSetActive"):
            logger.debug("SET_ACTIVE message missing srvrRndrSetActive field")
            return

        active = message.srvrRndrSetActive.active
        logger.info(f"Renderer set active: {active}")

        if active:
            # A controller just attached. The Qobuz cloud does not seem to replay
            # our last RndrSrvrVolumeChanged to it, so the device picker can show
            # an empty volume bar until we re-emit. Push current volume now.
            await self.player.broadcast_current_volume()
        else:
            # We're no longer the active renderer - stop playback
            await self.player.stop_playback()

    async def _handle_set_max_audio_quality(self, message: Any) -> None:
        """
        Handle SET_MAX_AUDIO_QUALITY message (type 44).

        The protocol uses different values: 1=MP3, 2=LOSSLESS, 3=HIRES_L1, 4=HIRES_L3
        We convert to Qobuz quality IDs: 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k
        """
        if not message.HasField("srvrRndrSetMaxAudioQuality"):
            return

        proto_quality = message.srvrRndrSetMaxAudioQuality.maxAudioQuality
        # Map protocol value to Qobuz quality ID
        protocol_to_quality = {1: 5, 2: 6, 3: 7, 4: 27}
        quality = protocol_to_quality.get(proto_quality, 27)

        logger.info(
            f"Max audio quality change requested: proto={proto_quality} -> qobuz_id={quality}"
        )

        if self._on_quality_change:
            await self._on_quality_change(quality)

    async def _handle_set_loop_mode(self, message: Any) -> None:
        """Handle SET_LOOP_MODE message (type 45)."""
        if not message.HasField("srvrRndrSetLoopMode"):
            return

        # Protocol LoopMode: 0=UNKNOWN, 1=OFF, 2=REPEAT_ONE, 3=REPEAT_ALL
        mode = message.srvrRndrSetLoopMode.mode
        logger.info(f"Loop mode set to: {mode}")
        await self.player.set_loop_mode(mode)

    async def _handle_set_shuffle_mode(self, message: Any) -> None:
        """Handle SET_SHUFFLE_MODE message (type 46)."""
        if not message.HasField("srvrRndrSetShuffleMode"):
            return

        shuffle_on = message.srvrRndrSetShuffleMode.shuffleOn
        logger.info(f"Shuffle mode set to: {shuffle_on}")
        await self.player.set_shuffle_mode(shuffle_on)

    async def _handle_set_autoplay_mode(self, message: Any) -> None:
        """Handle SET_AUTOPLAY_MODE message (type 47)."""
        if not message.HasField("srvrRndrSetAutoplayMode"):
            return

        autoplay_on = message.srvrRndrSetAutoplayMode.autoplayOn
        logger.info(f"Autoplay mode set to: {autoplay_on}")
        await self.player.set_autoplay_mode(autoplay_on)
