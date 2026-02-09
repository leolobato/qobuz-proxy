"""
Queue message handler for WebSocket integration.

Processes queue-related messages from the Qobuz app via WsManager.
"""

import logging
from typing import Any

from qobuz_proxy.connect.protocol import QConnectMessageType

from .queue import QobuzQueue, QueueVersion, RepeatMode

logger = logging.getLogger(__name__)


class QueueHandler:
    """
    Handles queue-related messages from WebSocket.

    Translates protobuf messages to queue operations.
    """

    def __init__(self, queue: QobuzQueue):
        """Initialize queue handler."""
        self.queue = queue

    def get_message_types(self) -> list[int]:
        """Get list of message types this handler processes."""
        return [
            QConnectMessageType.SRVR_CTRL_QUEUE_STATE,
            QConnectMessageType.SRVR_CTRL_QUEUE_TRACKS_LOADED,
        ]

    async def handle_message(self, msg_type: int, message: Any) -> None:
        """Handle a queue-related message."""
        try:
            if msg_type == QConnectMessageType.SRVR_CTRL_QUEUE_STATE:
                await self._handle_queue_state(message)
            elif msg_type == QConnectMessageType.SRVR_CTRL_QUEUE_TRACKS_LOADED:
                await self._handle_queue_tracks_loaded(message)
            else:
                logger.warning(f"Unhandled queue message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error handling queue message {msg_type}: {e}", exc_info=True)

    async def _handle_queue_state(self, message: Any) -> None:
        """
        Handle SRVR_CTRL_QUEUE_STATE message (full queue state).

        This is sent when we first connect or when requesting full state.
        """
        queue_state = message.srvrCtrlQueueState

        # Extract version
        version = QueueVersion(
            major=queue_state.queueVersion.major,
            minor=queue_state.queueVersion.minor,
        )

        # Extract tracks
        tracks = []
        for track_ref in queue_state.tracks:
            tracks.append(
                {
                    "queueItemId": track_ref.queueItemId,
                    "trackId": track_ref.trackId,
                    "contextUuid": (track_ref.contextUuid if track_ref.contextUuid else None),
                }
            )

        # Load queue
        await self.queue.load_queue(tracks=tracks, version=version)

        # Apply shuffle mode if present
        if queue_state.shuffleMode:
            # Use shuffled indexes from server
            await self.queue.set_shuffle(
                enabled=True,
                pivot_item_id=None,  # Server already shuffled
            )
            # Note: The actual shuffled order comes from shuffledTrackIndexes
            # but our implementation re-shuffles; for full fidelity we'd
            # need to apply the server's shuffle order

        logger.info(f"Queue state received: {len(tracks)} tracks, version {version}")

    async def _handle_queue_tracks_loaded(self, message: Any) -> None:
        """
        Handle SRVR_CTRL_QUEUE_TRACKS_LOADED message.

        This is sent when tracks are loaded into the queue (play command).
        """
        load_msg = message.srvrCtrlQueueLoadTracks

        # Extract version
        version = QueueVersion(
            major=load_msg.queueVersion.major,
            minor=load_msg.queueVersion.minor,
        )

        # Extract tracks
        tracks = []
        for track_ref in load_msg.tracks:
            tracks.append(
                {
                    "queueItemId": track_ref.queueItemId,
                    "trackId": track_ref.trackId,
                    "contextUuid": (track_ref.contextUuid if track_ref.contextUuid else None),
                }
            )

        # Determine current track from queue position
        queue_position = load_msg.queuePosition if load_msg.queuePosition else 0
        current_item_id = None
        if tracks and queue_position < len(tracks):
            current_item_id = tracks[queue_position]["queueItemId"]

        # Load queue
        await self.queue.load_queue(
            tracks=tracks,
            version=version,
            current_item_id=current_item_id,
        )

        # Apply shuffle mode if present
        if load_msg.shuffleMode:
            pivot_id = (
                load_msg.shufflePivotQueueItemId
                if load_msg.shufflePivotQueueItemId
                else current_item_id
            )
            await self.queue.set_shuffle(enabled=True, pivot_item_id=pivot_id)

        logger.info(
            f"Queue loaded: {len(tracks)} tracks, version {version}, " f"position {queue_position}"
        )

    async def handle_set_state(
        self,
        current_queue_item_id: int | None = None,
        shuffle_mode: bool | None = None,
        loop_mode: int | None = None,
    ) -> None:
        """
        Handle state changes from SET_STATE messages.

        This is called by the playback handler when processing SET_STATE
        messages that include queue-related fields.

        Args:
            current_queue_item_id: New current track in queue
            shuffle_mode: Shuffle enabled/disabled
            loop_mode: Loop mode (0=off, 1=one, 2=all)
        """
        # Update current queue position if specified
        if current_queue_item_id is not None:
            await self.queue.set_current_by_item_id(current_queue_item_id)

        # Update shuffle mode
        if shuffle_mode is not None:
            current_state = await self.queue.get_state()
            if shuffle_mode != current_state.shuffle_enabled:
                pivot_id = current_state.current_queue_item_id if shuffle_mode else None
                await self.queue.set_shuffle(shuffle_mode, pivot_item_id=pivot_id)

        # Update loop mode
        if loop_mode is not None:
            mode = self._map_loop_mode(loop_mode)
            await self.queue.set_repeat_mode(mode)

    def _map_loop_mode(self, proto_mode: int) -> RepeatMode:
        """Map protobuf loop mode to RepeatMode enum."""
        mapping = {
            0: RepeatMode.OFF,  # LOOP_MODE_UNKNOWN or OFF
            1: RepeatMode.OFF,  # LOOP_MODE_OFF
            2: RepeatMode.ONE,  # LOOP_MODE_REPEAT_ONE
            3: RepeatMode.ALL,  # LOOP_MODE_REPEAT_ALL
        }
        return mapping.get(proto_mode, RepeatMode.OFF)
