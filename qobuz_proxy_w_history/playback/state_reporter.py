"""
State reporter for Qobuz Connect protocol.

Handles periodic and event-driven state updates to the Qobuz app.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

from qobuz_proxy.backends import PlaybackState, BufferStatus

if TYPE_CHECKING:
    from .player import QobuzPlayer
    from .queue import QobuzQueue

logger = logging.getLogger(__name__)

# State update interval (matches C++ heartbeat)
STATE_UPDATE_INTERVAL_SECONDS = 5.0


@dataclass
class PlaybackStateReport:
    """
    Complete playback state for reporting to Qobuz app.

    All fields required by the QueueRendererState protobuf message.
    """

    # Playback state
    playing_state: PlaybackState
    buffer_state: BufferStatus

    # Position tracking
    position_timestamp_ms: int  # When position was recorded
    position_value_ms: int  # Position value at timestamp
    duration_ms: int

    # Queue info
    current_queue_item_id: int
    queue_version_major: int
    queue_version_minor: int

    def to_proto_dict(self) -> dict:
        """Convert to dictionary matching protobuf structure."""
        # Protocol only supports: 1=STOPPED, 2=PLAYING, 3=PAUSED
        # Map internal LOADING (4) and ERROR (5) to valid protocol values
        playing_state = self.playing_state
        if playing_state == PlaybackState.LOADING:
            playing_state = PlaybackState.STOPPED  # Loading shown as stopped
        elif playing_state == PlaybackState.ERROR:
            playing_state = PlaybackState.STOPPED  # Error shown as stopped

        return {
            "playingState": int(playing_state),
            "bufferState": int(self.buffer_state),
            "currentPosition": {
                "timestamp": self.position_timestamp_ms,
                "value": self.position_value_ms,
            },
            "duration": self.duration_ms,
            "currentQueueItemId": self.current_queue_item_id,
            "queueVersion": {
                "major": self.queue_version_major,
                "minor": self.queue_version_minor,
            },
        }


# Type alias for send callback
SendCallback = Callable[["PlaybackStateReport"], "asyncio.Future[None]"]


class StateReporter:
    """
    Manages state reporting to Qobuz app.

    Sends:
    - Periodic updates every 5 seconds (heartbeat)
    - Immediate updates on state changes

    Does NOT handle volume (separate RndrSrvrVolumeChanged message).
    """

    def __init__(
        self,
        player: "QobuzPlayer",
        queue: "QobuzQueue",
        send_callback: SendCallback,
    ):
        """
        Initialize state reporter.

        Args:
            player: Player instance for state access
            queue: Queue instance for queue state
            send_callback: Async callback to send state update
        """
        self._player = player
        self._queue = queue
        self._send_callback = send_callback

        self._is_running = False
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the state reporter heartbeat."""
        if self._is_running:
            return

        self._is_running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("StateReporter started")

    async def stop(self) -> None:
        """Stop the state reporter."""
        self._is_running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        logger.info("StateReporter stopped")

    async def report_now(self) -> None:
        """
        Send immediate state update.

        Call this when state changes:
        - Play/pause/stop
        - Track change
        - Seek complete
        - Shuffle/repeat mode change
        - Error occurred
        """
        await self._send_state_update()

    async def _heartbeat_loop(self) -> None:
        """Periodic state update loop."""
        while self._is_running:
            try:
                await asyncio.sleep(STATE_UPDATE_INTERVAL_SECONDS)

                # Only send heartbeat if playing (not stopped/paused)
                if self._player.state == PlaybackState.PLAYING:
                    await self._send_state_update()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}", exc_info=True)
                await asyncio.sleep(1.0)  # Brief pause before retry

    async def _send_state_update(self) -> None:
        """Build and send state update."""
        try:
            report = await self._build_state_report()
            await self._send_callback(report)
            logger.debug(
                f"State update sent: {report.playing_state.name}, "
                f"pos={report.position_value_ms}ms, ts={report.position_timestamp_ms}"
            )
        except Exception as e:
            logger.error(f"Failed to send state update: {e}", exc_info=True)

    async def _build_state_report(self) -> PlaybackStateReport:
        """Build current state report."""
        # Get queue state
        queue_state = await self._queue.get_state()

        # Get current track info
        current_track = self._player.current_track
        queue_item_id = current_track.queue_item_id if current_track else 0

        # Get position with current timestamp
        now_ms = int(time.time() * 1000)

        # For playing state, use timestamp-based position
        # For paused/stopped, use last known position
        if self._player.state == PlaybackState.PLAYING:
            position_timestamp = self._player._position_timestamp_ms
            position_value = self._player._position_value_ms
            logger.debug(
                f"Building report (PLAYING): player._position_value_ms={position_value}, "
                f"player._position_timestamp_ms={position_timestamp}"
            )
        else:
            # When paused/stopped, freeze position at current value
            position_timestamp = now_ms
            position_value = self._player.current_position_ms
            logger.debug(
                f"Building report ({self._player.state.name}): "
                f"player.current_position_ms={position_value}"
            )

        # Get buffer status from backend
        buffer_status = await self._player.backend.get_buffer_status()

        return PlaybackStateReport(
            playing_state=self._player.state,
            buffer_state=buffer_status,
            position_timestamp_ms=position_timestamp,
            position_value_ms=position_value,
            duration_ms=self._player.duration_ms,
            current_queue_item_id=queue_item_id,
            queue_version_major=queue_state.version.major,
            queue_version_minor=queue_state.version.minor,
        )
