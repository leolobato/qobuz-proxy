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
        if state.HasField("currentQueueItem"):
            current_item = state.currentQueueItem
            current_queue_item_id = current_item.queueItemId
            current_track_id = current_item.trackId
            logger.debug(
                f"Current queue item: queueItemId={current_queue_item_id}, trackId={current_track_id}"
            )

        # Extract and store next queue item for auto-advance
        if state.HasField("nextQueueItem"):
            next_item = state.nextQueueItem
            self._next_track_info = {
                "queueItemId": next_item.queueItemId,
                "trackId": str(next_item.trackId),
                "contextUuid": next_item.contextUuid if next_item.contextUuid else None,
            }
            logger.debug(
                f"Next track stored: queueItemId={next_item.queueItemId}, trackId={next_item.trackId}"
            )

        # Check if the app is telling us to play a different track than what we're playing
        player_track = self.player.current_track
        player_track_id = player_track.track_id if player_track else None

        if current_item and str(current_track_id) != player_track_id:
            # App wants us to play a different track - load it
            logger.info(f"Loading new track: {current_track_id}")
            await self.player.load_track(
                queue_item_id=current_queue_item_id,
                track_id=str(current_track_id),
            )

        # Extract position
        position_ms = 0
        if state.HasField("currentPosition"):
            position_ms = state.currentPosition
            logger.debug(f"Position: {position_ms}ms")

        # Handle playing state - apply AFTER track is loaded
        if state.HasField("playingState"):
            proto_state = state.playingState
            logger.debug(f"Playing state: {proto_state}")

            # Proto: 1=STOPPED, 2=PLAYING, 3=PAUSED
            if proto_state == 2:  # PLAYING
                await self.player.play(position_ms=position_ms)
            elif proto_state == 3:  # PAUSED
                await self.player.pause()
            elif proto_state == 1:  # STOPPED
                await self.player.stop_playback()

    def get_next_track_info(self) -> Optional[dict]:
        """Get the stored next track info for auto-advance."""
        return self._next_track_info

    def clear_next_track_info(self) -> None:
        """Clear the stored next track info after it's been used."""
        self._next_track_info = None

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

        if not active:
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
