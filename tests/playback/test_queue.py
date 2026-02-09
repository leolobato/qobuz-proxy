"""Tests for queue management."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.playback.queue import (
    QobuzQueue,
    QueueTrack,
    QueueVersion,
    RepeatMode,
)


class TestQueueVersion:
    """Tests for QueueVersion class."""

    def test_str(self) -> None:
        """Test string representation."""
        version = QueueVersion(major=5, minor=3)
        assert str(version) == "5.3"

    def test_is_newer_than_major(self) -> None:
        """Test version comparison by major."""
        v1 = QueueVersion(major=2, minor=0)
        v2 = QueueVersion(major=1, minor=5)
        assert v1.is_newer_than(v2) is True
        assert v2.is_newer_than(v1) is False

    def test_is_newer_than_minor(self) -> None:
        """Test version comparison by minor."""
        v1 = QueueVersion(major=1, minor=5)
        v2 = QueueVersion(major=1, minor=3)
        assert v1.is_newer_than(v2) is True
        assert v2.is_newer_than(v1) is False

    def test_is_newer_than_equal(self) -> None:
        """Test version comparison when equal."""
        v1 = QueueVersion(major=1, minor=5)
        v2 = QueueVersion(major=1, minor=5)
        assert v1.is_newer_than(v2) is False
        assert v2.is_newer_than(v1) is False


class TestQueueTrack:
    """Tests for QueueTrack class."""

    def test_defaults(self) -> None:
        """Test default values."""
        track = QueueTrack(queue_item_id=1, track_id="12345")
        assert track.queue_item_id == 1
        assert track.track_id == "12345"
        assert track.context_uuid is None
        assert track.streaming_url is None
        assert track.metadata == {}
        assert track.start_ms == 0
        assert track.duration_ms == 0


class TestQobuzQueue:
    """Tests for QobuzQueue class."""

    @pytest.fixture
    def queue(self) -> QobuzQueue:
        """Create a fresh queue."""
        return QobuzQueue()

    @pytest.fixture
    def sample_tracks(self) -> list[dict[str, Any]]:
        """Sample track data for testing."""
        return [
            {"queueItemId": 1, "trackId": "A"},
            {"queueItemId": 2, "trackId": "B"},
            {"queueItemId": 3, "trackId": "C"},
            {"queueItemId": 4, "trackId": "D"},
            {"queueItemId": 5, "trackId": "E"},
        ]

    # =========================================================================
    # Load Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_load_queue(self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]) -> None:
        """Test loading a queue with tracks."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version)

        state = await queue.get_state()
        assert state.track_count == 5
        assert state.version.major == 1
        assert state.current_index == 0

    @pytest.mark.asyncio
    async def test_load_queue_with_current_item(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test loading queue with specific current item."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version, current_item_id=3)

        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"
        assert track.queue_item_id == 3

    @pytest.mark.asyncio
    async def test_load_queue_clears_previous(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test that loading queue clears previous data."""
        version = QueueVersion(major=1, minor=0)
        await queue.load_queue(sample_tracks, version)

        # Load different queue
        new_tracks = [{"queueItemId": 10, "trackId": "X"}]
        await queue.load_queue(new_tracks, QueueVersion(major=2, minor=0))

        state = await queue.get_state()
        assert state.track_count == 1
        assert state.version.major == 2

    # =========================================================================
    # Navigation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_advance_to_next(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test advancing to next track."""
        await queue.load_queue(sample_tracks, QueueVersion())

        # Start at A
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "A"

        # Advance to B
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

        # Advance to C
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "C"

    @pytest.mark.asyncio
    async def test_go_to_previous(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test going to previous track."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=3)

        # Start at C
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Go back to B
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "B"

        # Go back to A
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "A"

    @pytest.mark.asyncio
    async def test_advance_at_end_no_repeat(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test advancing at end with repeat off returns None."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)

        track = await queue.advance_to_next()
        assert track is None

    @pytest.mark.asyncio
    async def test_previous_at_beginning(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test going previous at beginning stays at first track."""
        await queue.load_queue(sample_tracks, QueueVersion())

        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "A"

    # =========================================================================
    # Clear Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_clear_queue(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test clearing the queue."""
        await queue.load_queue(sample_tracks, QueueVersion())

        await queue.clear()

        assert queue.is_empty is True
        track = await queue.get_current_track()
        assert track is None

    # =========================================================================
    # Shuffle Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_enable_shuffle_preserves_current(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test enabling shuffle keeps current track at current position."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=2)

        # Current is B
        track_before = await queue.get_current_track()
        assert track_before is not None
        assert track_before.track_id == "B"

        # Enable shuffle with B as pivot
        await queue.set_shuffle(enabled=True, pivot_item_id=2)

        # B should still be current
        track_after = await queue.get_current_track()
        assert track_after is not None
        assert track_after.track_id == "B"

    @pytest.mark.asyncio
    async def test_disable_shuffle_restores_order(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test disabling shuffle restores original order."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=3)  # Start at C

        # Enable shuffle with C as pivot
        await queue.set_shuffle(enabled=True, pivot_item_id=3)

        # C should still be current
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Disable shuffle
        await queue.set_shuffle(enabled=False)

        # C should still be current but now at its original position
        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "C"

        # Navigate forward from C
        tracks = [track.track_id]
        track = await queue.advance_to_next()
        while track:
            tracks.append(track.track_id)
            track = await queue.advance_to_next()

        # Should be C, D, E (remaining in original order)
        assert tracks == ["C", "D", "E"]

    @pytest.mark.asyncio
    async def test_shuffle_state_reported(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test shuffle state is reported correctly."""
        await queue.load_queue(sample_tracks, QueueVersion())

        state = await queue.get_state()
        assert state.shuffle_enabled is False

        await queue.set_shuffle(enabled=True)

        state = await queue.get_state()
        assert state.shuffle_enabled is True

    # =========================================================================
    # Repeat Mode Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_repeat_one_stays_on_track(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat one mode stays on current track."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=2)
        await queue.set_repeat_mode(RepeatMode.ONE)

        # Advance should return same track
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "B"

    @pytest.mark.asyncio
    async def test_repeat_all_wraps_to_beginning(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat all wraps from end to beginning."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)
        await queue.set_repeat_mode(RepeatMode.ALL)

        # At E, advance should wrap to A
        track = await queue.advance_to_next()
        assert track is not None
        assert track.track_id == "A"

    @pytest.mark.asyncio
    async def test_repeat_all_previous_wraps_to_end(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat all wraps from beginning to end on previous."""
        await queue.load_queue(sample_tracks, QueueVersion())  # Start at A
        await queue.set_repeat_mode(RepeatMode.ALL)

        # At A, previous should wrap to E
        track = await queue.go_to_previous()
        assert track is not None
        assert track.track_id == "E"

    @pytest.mark.asyncio
    async def test_repeat_off_stops_at_end(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat off stops at end of queue."""
        await queue.load_queue(sample_tracks, QueueVersion(), current_item_id=5)
        await queue.set_repeat_mode(RepeatMode.OFF)

        # At E, advance should return None
        track = await queue.advance_to_next()
        assert track is None

    @pytest.mark.asyncio
    async def test_repeat_mode_state_reported(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test repeat mode is reported in state."""
        await queue.load_queue(sample_tracks, QueueVersion())

        state = await queue.get_state()
        assert state.repeat_mode == RepeatMode.OFF

        await queue.set_repeat_mode(RepeatMode.ALL)

        state = await queue.get_state()
        assert state.repeat_mode == RepeatMode.ALL

    # =========================================================================
    # Set Current By Item ID Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_set_current_by_item_id(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test setting current track by queue item ID."""
        await queue.load_queue(sample_tracks, QueueVersion())

        result = await queue.set_current_by_item_id(4)
        assert result is True

        track = await queue.get_current_track()
        assert track is not None
        assert track.track_id == "D"

    @pytest.mark.asyncio
    async def test_set_current_by_item_id_not_found(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test setting current to non-existent item ID."""
        await queue.load_queue(sample_tracks, QueueVersion())

        result = await queue.set_current_by_item_id(999)
        assert result is False

    # =========================================================================
    # Version Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_version_tracking(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test queue version is stored and retrieved."""
        version = QueueVersion(major=5, minor=3)
        await queue.load_queue(sample_tracks, version)

        stored_version = await queue.get_version()
        assert stored_version.major == 5
        assert stored_version.minor == 3

    @pytest.mark.asyncio
    async def test_set_version(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test updating queue version."""
        await queue.load_queue(sample_tracks, QueueVersion(major=1, minor=0))

        await queue.set_version(QueueVersion(major=2, minor=5))

        version = await queue.get_version()
        assert version.major == 2
        assert version.minor == 5

    # =========================================================================
    # Preloading Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_preload_fetches_metadata(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test preloading fetches metadata for upcoming tracks."""
        # Setup mock callbacks
        metadata_callback = AsyncMock(
            return_value={
                "title": "Test Track",
                "artist": "Test Artist",
                "duration_ms": 180000,
            }
        )
        url_callback = AsyncMock(return_value="https://streaming.example.com/track.flac")

        queue.set_metadata_callback(metadata_callback)
        queue.set_url_callback(url_callback)

        await queue.load_queue(sample_tracks, QueueVersion())
        await queue.start()

        # Wait for preload
        await asyncio.sleep(1.5)

        await queue.stop()

        # Check that callbacks were called for first few tracks
        assert metadata_callback.call_count >= 1
        assert url_callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_preload_skips_already_preloaded(
        self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]
    ) -> None:
        """Test preloading doesn't re-fetch already preloaded tracks."""
        metadata_callback = AsyncMock(return_value={"title": "Test"})
        url_callback = AsyncMock(return_value="https://example.com/track.flac")

        queue.set_metadata_callback(metadata_callback)
        queue.set_url_callback(url_callback)

        await queue.load_queue(sample_tracks, QueueVersion())
        await queue.start()

        # Wait for initial preload
        await asyncio.sleep(1.5)

        initial_metadata_calls = metadata_callback.call_count
        initial_url_calls = url_callback.call_count

        # Wait for another preload cycle
        await asyncio.sleep(1.5)

        await queue.stop()

        # Should not have called again for same tracks
        assert metadata_callback.call_count == initial_metadata_calls
        assert url_callback.call_count == initial_url_calls

    # =========================================================================
    # State Access Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_get_state(self, queue: QobuzQueue, sample_tracks: list[dict[str, Any]]) -> None:
        """Test getting queue state snapshot."""
        await queue.load_queue(sample_tracks, QueueVersion(major=3, minor=2), current_item_id=2)
        await queue.set_shuffle(enabled=True, pivot_item_id=2)
        await queue.set_repeat_mode(RepeatMode.ALL)

        state = await queue.get_state()

        assert state.version.major == 3
        assert state.version.minor == 2
        assert state.track_count == 5
        assert state.current_queue_item_id == 2
        assert state.shuffle_enabled is True
        assert state.repeat_mode == RepeatMode.ALL

    @pytest.mark.asyncio
    async def test_is_empty(self, queue: QobuzQueue) -> None:
        """Test is_empty property."""
        assert queue.is_empty is True

        await queue.load_queue([{"queueItemId": 1, "trackId": "A"}], QueueVersion())
        assert queue.is_empty is False

        await queue.clear()
        assert queue.is_empty is True

    # =========================================================================
    # Lifecycle Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_start_stop(self, queue: QobuzQueue) -> None:
        """Test starting and stopping preload task."""
        await queue.start()
        assert queue._is_running is True
        assert queue._preload_task is not None

        await queue.stop()
        assert queue._is_running is False
        assert queue._preload_task is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self, queue: QobuzQueue) -> None:
        """Test starting twice doesn't create multiple tasks."""
        await queue.start()
        task1 = queue._preload_task

        await queue.start()
        task2 = queue._preload_task

        assert task1 is task2

        await queue.stop()

    # =========================================================================
    # Empty Queue Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_operations_on_empty_queue(self, queue: QobuzQueue) -> None:
        """Test operations on empty queue don't crash."""
        track = await queue.get_current_track()
        assert track is None

        track = await queue.advance_to_next()
        assert track is None

        track = await queue.go_to_previous()
        assert track is None

        await queue.set_shuffle(enabled=True)  # Should not crash
        await queue.set_repeat_mode(RepeatMode.ALL)  # Should not crash
