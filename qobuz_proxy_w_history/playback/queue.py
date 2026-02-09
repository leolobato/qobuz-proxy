"""
Queue management for QobuzProxy.

Handles track ordering, shuffle, repeat, and preloading.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class RepeatMode(Enum):
    """Queue repeat modes."""

    OFF = "off"  # Stop after last track
    ONE = "one"  # Repeat current track
    ALL = "all"  # Loop entire queue


@dataclass
class QueueTrack:
    """
    Represents a track in the queue.

    Attributes:
        queue_item_id: Unique ID for this queue entry (from server)
        track_id: Qobuz track ID (for API calls)
        context_uuid: Optional context (album, playlist) UUID
        streaming_url: Cached streaming URL (may expire)
        metadata: Cached track metadata dict
        start_ms: Start position for partial plays
        duration_ms: Track duration in milliseconds
    """

    queue_item_id: int
    track_id: str
    context_uuid: Optional[bytes] = None
    streaming_url: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_ms: int = 0
    duration_ms: int = 0


@dataclass
class QueueVersion:
    """
    Queue version for synchronization.

    The server tracks queue state with major/minor version numbers.
    Major increments on structural changes (add/remove/reorder).
    Minor increments on metadata updates.
    """

    major: int = 0
    minor: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    def is_newer_than(self, other: "QueueVersion") -> bool:
        """Check if this version is newer than another."""
        if self.major != other.major:
            return self.major > other.major
        return self.minor > other.minor


@dataclass
class QueueState:
    """
    Snapshot of current queue state for reporting.

    Used when sending state updates to the Qobuz app.
    """

    version: QueueVersion
    track_count: int
    current_index: int
    current_queue_item_id: int
    shuffle_enabled: bool
    repeat_mode: RepeatMode


# Type alias for callbacks
UrlCallback = Callable[[str], Coroutine[Any, Any, Optional[str]]]
MetadataCallback = Callable[[str], Coroutine[Any, Any, Optional[dict[str, Any]]]]


class QobuzQueue:
    """
    Queue manager for QobuzProxy.

    Handles:
    - Track list management
    - Shuffle mode with pivot preservation
    - Repeat modes (off, one, all)
    - Preloading upcoming tracks
    - Version synchronization with server
    """

    # Number of tracks to preload ahead
    PRELOAD_COUNT = 3

    def __init__(self) -> None:
        """Initialize empty queue."""
        # Track storage
        self._tracks: list[QueueTrack] = []
        self._original_order: list[int] = []  # Track indexes in original order
        self._shuffled_indexes: list[int] = []  # Mapping: position -> track index

        # Position tracking
        self._current_index: int = 0  # Position in shuffled/ordered list

        # Mode settings
        self._shuffle_enabled: bool = False
        self._repeat_mode: RepeatMode = RepeatMode.OFF

        # Version tracking
        self._version: QueueVersion = QueueVersion()

        # Preloading
        self._preload_task: Optional[asyncio.Task[None]] = None
        self._preloaded_ids: set[int] = set()  # queue_item_ids that are preloaded

        # Callbacks for fetching URLs and metadata
        self._get_url_callback: Optional[UrlCallback] = None
        self._get_metadata_callback: Optional[MetadataCallback] = None

        # Thread safety
        self._lock = asyncio.Lock()

        # Running state
        self._is_running = False

        logger.debug("QobuzQueue initialized")

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def set_url_callback(self, callback: UrlCallback) -> None:
        """Set callback for fetching streaming URLs."""
        self._get_url_callback = callback

    def set_metadata_callback(self, callback: MetadataCallback) -> None:
        """Set callback for fetching track metadata."""
        self._get_metadata_callback = callback

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start queue preloading background task."""
        if self._is_running:
            return

        self._is_running = True
        self._preload_task = asyncio.create_task(self._preload_loop())
        logger.info("Queue preloading started")

    async def stop(self) -> None:
        """Stop queue preloading."""
        self._is_running = False
        if self._preload_task:
            self._preload_task.cancel()
            try:
                await self._preload_task
            except asyncio.CancelledError:
                pass
            self._preload_task = None
        logger.info("Queue preloading stopped")

    # =========================================================================
    # Queue Management
    # =========================================================================

    async def load_queue(
        self,
        tracks: list[dict[str, Any]],
        version: QueueVersion,
        current_item_id: Optional[int] = None,
    ) -> None:
        """
        Load a complete queue from the Qobuz app.

        Args:
            tracks: List of track dicts with queueItemId, trackId, etc.
            version: Queue version from server
            current_item_id: Queue item ID to set as current (optional)
        """
        async with self._lock:
            # Clear existing queue
            self._tracks.clear()
            self._original_order.clear()
            self._shuffled_indexes.clear()
            self._preloaded_ids.clear()
            self._current_index = 0

            # Load new tracks
            for i, track_data in enumerate(tracks):
                track = QueueTrack(
                    queue_item_id=track_data.get("queueItemId", i),
                    track_id=str(track_data.get("trackId", "")),
                    context_uuid=track_data.get("contextUuid"),
                    start_ms=track_data.get("startMs", 0),
                    duration_ms=track_data.get("durationMs", 0),
                )
                self._tracks.append(track)
                self._original_order.append(i)

            # Initialize index mapping (identity until shuffle enabled)
            self._shuffled_indexes = list(range(len(self._tracks)))

            # Update version
            self._version = version

            # Set current position
            if current_item_id is not None:
                self._set_index_by_item_id(current_item_id)

            logger.info(
                f"Loaded queue: {len(self._tracks)} tracks, "
                f"version {self._version}, "
                f"current index {self._current_index}"
            )

    async def clear(self) -> None:
        """Clear the entire queue."""
        async with self._lock:
            self._tracks.clear()
            self._original_order.clear()
            self._shuffled_indexes.clear()
            self._preloaded_ids.clear()
            self._current_index = 0
            self._version = QueueVersion()

        logger.info("Queue cleared")

    async def set_current_by_item_id(self, queue_item_id: int) -> bool:
        """Set current track by queue item ID."""
        async with self._lock:
            return self._set_index_by_item_id(queue_item_id)

    def _set_index_by_item_id(self, queue_item_id: int) -> bool:
        """Internal: Set index by item ID (must hold lock)."""
        # Find track index by queue_item_id
        track_index = None
        for i, track in enumerate(self._tracks):
            if track.queue_item_id == queue_item_id:
                track_index = i
                break

        if track_index is None:
            logger.warning(f"Queue item {queue_item_id} not found")
            return False

        # Find position in shuffled order
        try:
            new_index = self._shuffled_indexes.index(track_index)
            old_index = self._current_index
            self._current_index = new_index

            # Invalidate preloaded tracks that are no longer upcoming
            self._invalidate_stale_preloads()

            logger.debug(f"Queue index: {old_index} -> {new_index}")
            return True
        except ValueError:
            logger.error(f"Track index {track_index} not in shuffled indexes")
            return False

    # =========================================================================
    # Shuffle Mode
    # =========================================================================

    async def set_shuffle(self, enabled: bool, pivot_item_id: Optional[int] = None) -> None:
        """
        Enable or disable shuffle mode.

        Args:
            enabled: Whether to enable shuffle
            pivot_item_id: Track to keep at current position (usually current track)
        """
        async with self._lock:
            self._shuffle_enabled = enabled

            if enabled:
                self._apply_shuffle(pivot_item_id)
            else:
                self._restore_original_order()

            # Invalidate preloads since order changed
            self._preloaded_ids.clear()

        logger.info(f"Shuffle mode: {enabled}")

    def _apply_shuffle(self, pivot_item_id: Optional[int]) -> None:
        """Apply shuffle to queue (must hold lock)."""
        if not self._tracks:
            return

        # Create shuffled index list
        indexes = list(range(len(self._tracks)))
        random.shuffle(indexes)

        # If pivot specified, move it to current position
        if pivot_item_id is not None:
            # Find track index for pivot
            pivot_track_index = None
            for i, track in enumerate(self._tracks):
                if track.queue_item_id == pivot_item_id:
                    pivot_track_index = i
                    break

            if pivot_track_index is not None:
                # Find where pivot ended up in shuffle
                pivot_position = indexes.index(pivot_track_index)
                # Swap with current position
                current_pos = min(self._current_index, len(indexes) - 1)
                indexes[current_pos], indexes[pivot_position] = (
                    indexes[pivot_position],
                    indexes[current_pos],
                )

        self._shuffled_indexes = indexes
        logger.debug(f"Shuffle applied, indexes: {self._shuffled_indexes[:10]}...")

    def _restore_original_order(self) -> None:
        """Restore original order when shuffle disabled (must hold lock)."""
        if not self._tracks:
            return

        # Get current track's original index
        current_track_index = self._get_current_track_index()

        # Restore sequential order
        self._shuffled_indexes = list(range(len(self._tracks)))

        # Update current_index to point to same track in new order
        if current_track_index is not None:
            try:
                self._current_index = self._shuffled_indexes.index(current_track_index)
            except ValueError:
                self._current_index = 0

    # =========================================================================
    # Repeat Mode
    # =========================================================================

    async def set_repeat_mode(self, mode: RepeatMode) -> None:
        """Set repeat mode."""
        async with self._lock:
            self._repeat_mode = mode
        logger.info(f"Repeat mode: {mode.value}")

    # =========================================================================
    # Navigation
    # =========================================================================

    async def get_current_track(self) -> Optional[QueueTrack]:
        """Get the current track."""
        async with self._lock:
            return self._get_current_track()

    def _get_current_track(self) -> Optional[QueueTrack]:
        """Internal: Get current track (must hold lock)."""
        track_index = self._get_current_track_index()
        if track_index is not None:
            return self._tracks[track_index]
        return None

    def _get_current_track_index(self) -> Optional[int]:
        """Get index into _tracks for current position (must hold lock)."""
        if not self._tracks or not self._shuffled_indexes:
            return None
        if self._current_index >= len(self._shuffled_indexes):
            return None
        return self._shuffled_indexes[self._current_index]

    async def advance_to_next(self) -> Optional[QueueTrack]:
        """
        Advance to next track respecting repeat mode.

        Returns:
            Next track or None if at end (and repeat off)
        """
        async with self._lock:
            if not self._tracks:
                return None

            # Repeat one: stay on current track
            if self._repeat_mode == RepeatMode.ONE:
                return self._get_current_track()

            # Try to advance
            next_index = self._current_index + 1

            if next_index >= len(self._shuffled_indexes):
                # At end of queue
                if self._repeat_mode == RepeatMode.ALL:
                    # Wrap to beginning
                    self._current_index = 0
                    logger.info("Queue wrapped to beginning (repeat all)")
                else:
                    # End of queue, no repeat
                    logger.info("End of queue reached")
                    return None
            else:
                self._current_index = next_index

            track = self._get_current_track()
            if track:
                logger.debug(f"Advanced to track {track.track_id} at index {self._current_index}")
            return track

    async def go_to_previous(self) -> Optional[QueueTrack]:
        """
        Go to previous track respecting repeat mode.

        Note: The >3 second restart logic is handled by the player, not the queue.
        """
        async with self._lock:
            if not self._tracks:
                return None

            # Repeat one: stay on current track
            if self._repeat_mode == RepeatMode.ONE:
                return self._get_current_track()

            # Try to go back
            if self._current_index > 0:
                self._current_index -= 1
            elif self._repeat_mode == RepeatMode.ALL:
                # Wrap to end
                self._current_index = len(self._shuffled_indexes) - 1
                logger.info("Queue wrapped to end (repeat all)")
            else:
                # At beginning, no repeat
                logger.info("At beginning of queue")
                return self._get_current_track()  # Return first track

            track = self._get_current_track()
            if track:
                logger.debug(f"Went back to track {track.track_id} at index {self._current_index}")
            return track

    # =========================================================================
    # Preloading
    # =========================================================================

    async def _preload_loop(self) -> None:
        """Background task to preload upcoming tracks."""
        while self._is_running:
            try:
                await self._preload_upcoming()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Preload error: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _preload_upcoming(self) -> None:
        """Preload metadata and URLs for upcoming tracks."""
        tracks_to_preload: list[QueueTrack] = []

        async with self._lock:
            for i in range(self.PRELOAD_COUNT):
                idx = self._current_index + i
                if idx >= len(self._shuffled_indexes):
                    break

                track_index = self._shuffled_indexes[idx]
                track = self._tracks[track_index]

                # Skip if already preloaded
                if track.queue_item_id in self._preloaded_ids:
                    continue

                tracks_to_preload.append(track)

        # Preload outside lock
        for track in tracks_to_preload:
            try:
                # Fetch metadata if missing
                if not track.metadata and self._get_metadata_callback:
                    metadata = await self._get_metadata_callback(track.track_id)
                    if metadata:
                        track.metadata = metadata
                        track.duration_ms = metadata.get("duration_ms", 0)
                        logger.debug(
                            f"Preloaded metadata for track {track.track_id}: "
                            f"{metadata.get('artist', '?')} - {metadata.get('title', '?')}"
                        )

                # Fetch URL if missing
                if not track.streaming_url and self._get_url_callback:
                    url = await self._get_url_callback(track.track_id)
                    if url:
                        track.streaming_url = url
                        logger.debug(f"Preloaded URL for track {track.track_id}")

                # Mark as preloaded
                async with self._lock:
                    self._preloaded_ids.add(track.queue_item_id)

            except Exception as e:
                logger.warning(f"Failed to preload track {track.track_id}: {e}")

    def _invalidate_stale_preloads(self) -> None:
        """Remove preloaded tracks that are no longer upcoming (must hold lock)."""
        if not self._preloaded_ids:
            return

        # Get expected upcoming queue_item_ids
        expected_ids: set[int] = set()
        for i in range(self.PRELOAD_COUNT + 1):  # +1 to include current
            idx = self._current_index + i
            if idx >= len(self._shuffled_indexes):
                break
            track_index = self._shuffled_indexes[idx]
            expected_ids.add(self._tracks[track_index].queue_item_id)

        # Keep only expected
        stale = self._preloaded_ids - expected_ids
        if stale:
            self._preloaded_ids -= stale
            logger.debug(f"Invalidated {len(stale)} stale preloaded tracks")

    # =========================================================================
    # State Access
    # =========================================================================

    async def get_state(self) -> QueueState:
        """Get current queue state snapshot."""
        async with self._lock:
            current_track = self._get_current_track()
            return QueueState(
                version=self._version,
                track_count=len(self._tracks),
                current_index=self._current_index,
                current_queue_item_id=current_track.queue_item_id if current_track else 0,
                shuffle_enabled=self._shuffle_enabled,
                repeat_mode=self._repeat_mode,
            )

    @property
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return len(self._tracks) == 0

    async def get_version(self) -> QueueVersion:
        """Get current queue version."""
        async with self._lock:
            return self._version

    async def set_version(self, version: QueueVersion) -> None:
        """Update queue version."""
        async with self._lock:
            self._version = version
