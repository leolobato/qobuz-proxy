"""Tests for QobuzPlayer gapless re-arming."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends import PlaybackState
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QueueTrack, RepeatMode


def _make_player(next_track_info=None):
    """Build a player with mocked queue/metadata/backend."""
    queue = MagicMock()
    metadata = MagicMock()
    metadata.get_streaming_url = AsyncMock(return_value="http://proxy:7120/audio/222_9.flac")
    meta_obj = MagicMock()
    meta_obj.to_dict.return_value = {
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "duration_ms": 1000,
        "artwork_url": "",
    }
    metadata.get_metadata = AsyncMock(return_value=meta_obj)
    metadata.get_track_format.return_value = (6, 44100, 16)

    backend = MagicMock()
    backend.supports_gapless = True
    backend.clear_next_track = AsyncMock()
    backend.set_next_track = AsyncMock(return_value=True)

    player = QobuzPlayer(queue=queue, metadata_service=metadata, backend=backend)
    player.set_next_track_callbacks(
        get_callback=lambda: next_track_info,
        clear_callback=lambda: None,
    )
    return player, backend


class TestOnNextTrackInfoChanged:
    """Queue edits mid-track (e.g. 'play next' in the app) must re-arm gapless."""

    async def test_rearms_with_new_next_track_while_playing(self):
        new_next = {"trackId": "222", "queueItemId": 9}
        player, backend = _make_player(next_track_info=new_next)
        player._state = PlaybackState.PLAYING
        # A stale track armed before the queue edit
        player._gapless_armed = True
        player._pending_next_track = {"trackId": "111", "queueItemId": 8}

        await player.on_next_track_info_changed()

        backend.clear_next_track.assert_awaited_once()
        backend.set_next_track.assert_awaited_once()
        assert player._pending_next_track["trackId"] == "222"
        assert player._gapless_armed is True

    async def test_clears_stale_arming_when_not_playing(self):
        player, backend = _make_player(next_track_info=None)
        player._state = PlaybackState.PAUSED
        player._gapless_armed = True
        player._pending_next_track = {"trackId": "111", "queueItemId": 8}

        await player.on_next_track_info_changed()

        backend.clear_next_track.assert_awaited_once()
        backend.set_next_track.assert_not_called()
        assert player._gapless_armed is False
        assert player._pending_next_track is None

    async def test_noop_when_armed_next_track_unchanged(self):
        """Redundant change events for the already-armed track must not re-arm.

        Re-arming appends a duplicate entry to the Sonos queue, which makes
        the song play twice.
        """
        same_next = {"trackId": "222", "queueItemId": 9}
        player, backend = _make_player(next_track_info=same_next)
        player._state = PlaybackState.PLAYING
        player._gapless_armed = True
        player._pending_next_track = {"trackId": "222", "queueItemId": 9}

        await player.on_next_track_info_changed()

        backend.clear_next_track.assert_not_called()
        backend.set_next_track.assert_not_called()
        assert player._gapless_armed is True


class TestPrepareNextTrackConcurrency:
    """Arming must be serialized — overlapping calls double-queue the next track."""

    async def test_concurrent_prepare_arms_backend_once(self):
        next_info = {"trackId": "222", "queueItemId": 9}
        player, backend = _make_player(next_track_info=next_info)
        player._state = PlaybackState.PLAYING

        async def slow_arm(*args, **kwargs):
            await asyncio.sleep(0.05)
            return True

        backend.set_next_track = AsyncMock(side_effect=slow_arm)

        await asyncio.gather(
            player._prepare_next_track_for_gapless(),
            player._prepare_next_track_for_gapless(),
        )

        backend.set_next_track.assert_awaited_once()
        assert player._gapless_armed is True

    async def test_stale_arm_undone_when_state_cleared_mid_arm(self):
        """A skip/stop while an arm is in flight must discard the stale arm."""
        next_info = {"trackId": "222", "queueItemId": 9}
        player, backend = _make_player(next_track_info=next_info)
        player._state = PlaybackState.PLAYING

        async def slow_arm(*args, **kwargs):
            await asyncio.sleep(0.05)
            return True

        backend.set_next_track = AsyncMock(side_effect=slow_arm)

        task = asyncio.create_task(player._prepare_next_track_for_gapless())
        await asyncio.sleep(0.01)  # let the arm reach the backend call
        player._clear_gapless_state()
        await task

        assert player._gapless_armed is False
        assert player._pending_next_track is None
        backend.clear_next_track.assert_awaited()


class TestContextUuidPropagation:
    """The album/playlist context UUID must reach the played track for scrobbles."""

    async def test_apply_remote_state_sets_context_uuid(self):
        player, backend = _make_player()
        backend.stop = AsyncMock()
        backend.seek = AsyncMock()
        ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=None,
            playing_state=None,
            context_uuid=ctx,
        )

        assert player._current_track.context_uuid == ctx

    async def test_load_track_defaults_context_to_none(self):
        player, backend = _make_player()
        backend.stop = AsyncMock()

        await player._load_track_locked(9, "222")

        assert player._current_track.context_uuid is None

    async def test_play_report_carries_formatted_context_uuid(self):
        player, backend = _make_player()
        backend.play = AsyncMock()
        backend.stop = AsyncMock()
        backend.seek = AsyncMock()
        reporter = MagicMock()
        reporter.note_playing = AsyncMock()
        reporter.note_stopped = AsyncMock()
        player._play_reporter = reporter
        player.metadata.get_track_actual_quality.return_value = 6
        player.metadata.get_track_blob.return_value = "blob"

        ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=0,
            playing_state=2,  # PLAYING
            context_uuid=ctx,
        )

        reporter.note_playing.assert_awaited()
        assert (
            reporter.note_playing.await_args.kwargs["context_uuid"]
            == "12345678-1234-5678-1234-567812345678"
        )

    async def test_same_track_set_state_updates_late_context(self):
        """A later SET_STATE for the already-loaded track must adopt its context."""
        player, backend = _make_player()
        backend.stop = AsyncMock()
        backend.seek = AsyncMock()
        # First SET_STATE: same track, no context yet.
        player._current_track = QueueTrack(queue_item_id=9, track_id="222")
        ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=None,
            playing_state=None,
            context_uuid=ctx,
        )

        assert player._current_track.context_uuid == ctx

    async def test_same_track_context_less_set_state_keeps_context(self):
        """A context-less SET_STATE must not wipe a known context."""
        player, backend = _make_player()
        backend.stop = AsyncMock()
        backend.seek = AsyncMock()
        ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        player._current_track = QueueTrack(queue_item_id=9, track_id="222", context_uuid=ctx)

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=None,
            playing_state=None,
            context_uuid=None,
        )

        assert player._current_track.context_uuid == ctx

    async def test_stale_pause_snapshot_does_not_overwrite_context(self):
        """A stale reconnect snapshot must not replace the live play context."""
        player, backend = _make_player()
        backend.stop = AsyncMock()
        backend.seek = AsyncMock()
        live_ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        stale_ctx = uuid.UUID("99999999-9999-9999-9999-999999999999").bytes

        # Live: playing the track at ~60s with the real context.
        player._current_track = QueueTrack(queue_item_id=9, track_id="222", context_uuid=live_ctx)
        player._state = PlaybackState.PLAYING
        player._position_value_ms = 60_000
        player._position_timestamp_ms = 0  # avoid time-based interpolation drift

        reporter = MagicMock()
        reporter.update_context = MagicMock()
        player._play_reporter = reporter

        # Stale PAUSED snapshot: same track, far-behind position, different context.
        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=1_000,
            playing_state=3,  # PAUSED
            context_uuid=stale_ctx,
        )

        assert player._current_track.context_uuid == live_ctx
        reporter.update_context.assert_not_called()

    async def test_next_track_context_change_triggers_rearm(self):
        """A changed next-track context must re-arm gapless, not no-op."""
        ctx_old = uuid.UUID("11111111-1111-1111-1111-111111111111").bytes
        ctx_new = uuid.UUID("22222222-2222-2222-2222-222222222222").bytes
        new_next = {"trackId": "222", "queueItemId": 9, "contextUuid": ctx_new}
        player, backend = _make_player(next_track_info=new_next)
        player._state = PlaybackState.PLAYING
        player._gapless_armed = True
        # Armed for the same track/queue item but with the old context.
        player._pending_next_track = {
            "trackId": "222",
            "queueItemId": 9,
            "contextUuid": ctx_old,
        }

        await player.on_next_track_info_changed()

        # Re-armed (cleared then prepared) rather than kept stale.
        backend.clear_next_track.assert_awaited_once()

    async def test_gapless_transition_carries_context_uuid(self):
        player, backend = _make_player()
        player._gapless_armed = True
        ctx = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        player._pending_next_track = {
            "trackId": "222",
            "queueItemId": 9,
            "contextUuid": ctx,
            "url": "http://proxy:7120/audio/222_9.flac",
            "metadata": {"duration_ms": 1000},
            "backend_meta": None,
        }
        player.metadata.get_track_actual_quality.return_value = 6

        await player._handle_gapless_transition()

        assert player._current_track.context_uuid == ctx


class TestSkipReusesArmedNextTrack:
    """Manual skip onto the gapless-armed next track must keep its prefetch."""

    def _arm_playing(self, player, backend):
        backend.stop = AsyncMock()
        backend.play = AsyncMock()
        backend.seek = AsyncMock()
        player._state = PlaybackState.PLAYING
        player._current_track = QueueTrack(queue_item_id=8, track_id="111")
        player._gapless_armed = True
        player._pending_next_track = {
            "trackId": "222",
            "queueItemId": 9,
            "url": "http://prefetch/222.flac",
            "metadata": {"title": "Next", "duration_ms": 180000},
            "backend_meta": None,
        }

    async def test_apply_remote_state_does_not_stop_or_refetch(self):
        player, backend = _make_player()
        self._arm_playing(player, backend)

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=0,
            playing_state=2,
        )

        backend.stop.assert_not_awaited()
        player.metadata.get_streaming_url.assert_not_awaited()
        backend.play.assert_awaited_once()
        backend.seek.assert_not_awaited()
        assert player._current_track.track_id == "222"
        assert player._current_track.streaming_url == "http://prefetch/222.flac"
        assert player._gapless_armed is False
        assert player._pending_next_track is None
        assert player._state == PlaybackState.PLAYING

    async def test_skip_to_other_track_still_stops(self):
        player, backend = _make_player()
        self._arm_playing(player, backend)

        await player.apply_remote_state(
            track_id="333",
            queue_item_id=10,
            position_ms=0,
            playing_state=2,
        )

        backend.stop.assert_awaited()
        player.metadata.get_streaming_url.assert_awaited()
        assert player._gapless_armed is False

    async def test_next_track_reuses_armed_prefetch(self):
        player, backend = _make_player()
        self._arm_playing(player, backend)
        next_track = QueueTrack(queue_item_id=9, track_id="222")
        player.queue.advance_to_next = AsyncMock(return_value=next_track)

        result = await player.next_track()

        assert result is True
        backend.stop.assert_not_awaited()
        backend.play.assert_awaited_once()
        assert player._current_track.streaming_url == "http://prefetch/222.flac"
        assert player._gapless_armed is False


class TestSkipAcknowledgesImmediately:
    """The Qobuz app treats renderer currentQueueItemId as authority.

    A skip that keeps advertising the outgoing track while stop/URL I/O runs
    makes the app snap back to whatever is still playing on the speaker.
    """

    async def test_new_item_is_current_before_stop_finishes(self):
        player, backend = _make_player()
        backend.play = AsyncMock()
        backend.seek = AsyncMock()
        player._current_track = QueueTrack(queue_item_id=8, track_id="111")
        player._state = PlaybackState.PLAYING

        seen_during_stop = []

        async def slow_stop():
            cur = player.current_track
            seen_during_stop.append(
                (
                    player.state,
                    cur.track_id if cur else None,
                    cur.queue_item_id if cur else None,
                )
            )
            await asyncio.sleep(0.01)

        backend.stop = AsyncMock(side_effect=slow_stop)

        reports = []

        async def capture_report():
            cur = player.current_track
            reports.append(
                (
                    player.state,
                    cur.track_id if cur else None,
                    player.reported_queue_item_id,
                )
            )

        player._state_update_callback = capture_report

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=0,
            playing_state=2,
        )

        assert seen_during_stop
        assert seen_during_stop[0] == (PlaybackState.LOADING, "222", 9)
        assert reports
        assert reports[0][2] == 9
        assert player.current_track is not None
        assert player.current_track.track_id == "222"
        assert player.current_track.queue_item_id == 9

    async def test_skip_override_wins_while_previous_load_holds_lock(self):
        player, backend = _make_player()
        backend.play = AsyncMock()
        backend.seek = AsyncMock()
        backend.stop = AsyncMock()
        player._current_track = QueueTrack(queue_item_id=8, track_id="111")
        player._state = PlaybackState.PLAYING

        async def slow_url(track_id: str) -> str:
            await asyncio.sleep(0.05)
            return f"http://test/{track_id}"

        player.metadata.get_streaming_url = MagicMock(side_effect=slow_url)

        first = asyncio.create_task(
            player.apply_remote_state(
                track_id="222",
                queue_item_id=9,
                position_ms=0,
                playing_state=2,
            )
        )
        await asyncio.sleep(0.01)  # first skip is fetching the URL, holding the lock

        second = asyncio.create_task(
            player.apply_remote_state(
                track_id="333",
                queue_item_id=10,
                position_ms=0,
                playing_state=2,
            )
        )
        await asyncio.sleep(0.01)  # let the second skip publish its override
        assert player.reported_queue_item_id == 10

        await asyncio.gather(first, second)
        assert player.current_track is not None
        assert player.current_track.track_id == "333"
        assert player.reported_queue_item_id == 10

    async def test_armed_skip_reports_new_item_before_play(self):
        player, backend = _make_player()
        backend.stop = AsyncMock()
        backend.play = AsyncMock()
        backend.seek = AsyncMock()
        player._current_track = QueueTrack(queue_item_id=8, track_id="111")
        player._state = PlaybackState.PLAYING
        player._gapless_armed = True
        player._pending_next_track = {
            "trackId": "222",
            "queueItemId": 9,
            "url": "http://prefetch/222.flac",
            "metadata": {"title": "Next", "duration_ms": 180000},
            "backend_meta": None,
        }

        reports = []

        async def capture_report():
            reports.append(player.reported_queue_item_id)

        player._state_update_callback = capture_report

        await player.apply_remote_state(
            track_id="222",
            queue_item_id=9,
            position_ms=0,
            playing_state=2,
        )

        assert reports
        assert reports[0] == 9
        backend.stop.assert_not_awaited()
        backend.play.assert_awaited_once()

    async def test_outgoing_track_echo_does_not_undo_armed_skip(self):
        """A SET_STATE for the previous song while the skip is loading must not
        bump generation and abort playback of the new track."""
        player, backend = _make_player()
        backend.stop = AsyncMock()

        async def slow_play(*args, **kwargs):
            await asyncio.sleep(0.05)

        backend.play = AsyncMock(side_effect=slow_play)
        backend.seek = AsyncMock()
        player._current_track = QueueTrack(queue_item_id=8, track_id="111")
        player._state = PlaybackState.PLAYING
        player._gapless_armed = True
        player._pending_next_track = {
            "trackId": "222",
            "queueItemId": 9,
            "url": "http://prefetch/222.flac",
            "metadata": {"title": "Next", "duration_ms": 180000},
            "backend_meta": None,
        }

        skip = asyncio.create_task(
            player.apply_remote_state(
                track_id="222",
                queue_item_id=9,
                position_ms=0,
                playing_state=2,
            )
        )
        for _ in range(50):
            if player.state == PlaybackState.LOADING and player._skipped_from_track_id == "111":
                break
            await asyncio.sleep(0.001)
        echo = asyncio.create_task(
            player.apply_remote_state(
                track_id="111",
                queue_item_id=8,
                position_ms=40_000,
                playing_state=2,
            )
        )
        await asyncio.gather(skip, echo)

        assert player.current_track is not None
        assert player.current_track.track_id == "222"
        assert player.state == PlaybackState.PLAYING
        backend.play.assert_awaited_once()
        player.metadata.get_streaming_url.assert_not_awaited()


class TestNextAtEndOfQueue:
    """Skipping past the end of the queue must still report the finished play."""

    async def test_reports_stopped_at_end_of_queue(self):
        player, backend = _make_player()
        backend.stop = AsyncMock()
        reporter = MagicMock()
        reporter.note_playing = AsyncMock()
        reporter.note_stopped = AsyncMock()
        player._play_reporter = reporter

        player._current_track = QueueTrack(queue_item_id=9, track_id="222")
        player._state = PlaybackState.PLAYING
        player.queue.advance_to_next = AsyncMock(return_value=None)  # end of queue

        result = await player.next_track()

        assert result is False
        assert player._state == PlaybackState.STOPPED
        # The finished play is reported so it scrobbles and the session closes.
        reporter.note_stopped.assert_awaited_once()


class TestResumeChecksBackend:
    """Resume must not report PLAYING unless the backend actually resumed."""

    async def test_failed_resume_stays_paused(self):
        player, backend = _make_player()
        backend.resume = AsyncMock(return_value=False)
        player._send_state_update = AsyncMock()
        player._current_track = QueueTrack(queue_item_id=9, track_id="222")
        player._state = PlaybackState.PAUSED

        result = await player.play()

        assert result is False
        assert player._state == PlaybackState.PAUSED
        # The real PAUSED state is pushed so the app corrects immediately.
        player._send_state_update.assert_awaited()

    async def test_successful_resume_goes_playing(self):
        player, backend = _make_player()
        backend.resume = AsyncMock(return_value=True)
        player._current_track = QueueTrack(queue_item_id=9, track_id="222")
        player._state = PlaybackState.PAUSED

        result = await player.play()

        assert result is True
        assert player._state == PlaybackState.PLAYING


class TestRepeatOneNaturalEnd:
    """Repeat-one must re-issue play; the backend is already STOPPED on natural end."""

    def _arm_repeat_one(self, player, backend):
        backend.play = AsyncMock()
        backend.seek = AsyncMock()
        backend.stop = AsyncMock()

        player._current_track = QueueTrack(
            queue_item_id=9,
            track_id="222",
            streaming_url="http://proxy:7120/audio/222_9.flac",
        )
        player._state = PlaybackState.PLAYING

        queue_state = MagicMock()
        queue_state.repeat_mode = RepeatMode.ONE
        player.queue.get_state = AsyncMock(return_value=queue_state)

    async def test_restarts_playback_on_natural_end(self):
        player, backend = _make_player()
        self._arm_repeat_one(player, backend)

        await player._handle_track_ended(player._current_track)

        # Audio must actually restart — a bare seek(0) on a stopped renderer is silent.
        backend.play.assert_awaited_once()
        assert player._state == PlaybackState.PLAYING
        # The position base is reset to 0 (the live clock interpolates from here,
        # so assert the stored base rather than the timing-sensitive clock).
        assert player._position_value_ms == 0

    async def test_refetches_url_so_repeat_does_not_use_expired_link(self):
        player, backend = _make_player()
        self._arm_repeat_one(player, backend)

        await player._handle_track_ended(player._current_track)

        # Cached URL is cleared and re-fetched so a long repeat loop never
        # plays through an expired streaming link.
        player.metadata.get_streaming_url.assert_awaited()

    async def test_does_not_advance_to_next_track(self):
        next_info = {"trackId": "999", "queueItemId": 42}
        player, backend = _make_player(next_track_info=next_info)
        self._arm_repeat_one(player, backend)

        await player._handle_track_ended(player._current_track)

        # The armed next track must be ignored under repeat-one.
        assert player._current_track.track_id == "222"
        # Stale gapless arming is dropped on natural end.
        assert player._gapless_armed is False
        assert player._pending_next_track is None

    async def test_restart_yields_to_user_stop(self):
        """A user Stop that raced the natural end must not be overridden.

        Stop leaves _current_track set but flips state to STOPPED, so the
        restart must detect the state change and bail.
        """
        player, backend = _make_player()
        self._arm_repeat_one(player, backend)
        ended_track = player._current_track

        # Stop ran while we were reporting: same track, but state is STOPPED.
        player._state = PlaybackState.STOPPED

        await player._restart_current_track(ended_track)

        backend.play.assert_not_awaited()

    async def test_restart_yields_to_user_next(self):
        """A user Next that swapped the current track must not be overridden."""
        player, backend = _make_player()
        self._arm_repeat_one(player, backend)
        ended_track = player._current_track

        # Next ran while we were reporting: a different track is now current.
        player._current_track = QueueTrack(queue_item_id=10, track_id="333")

        await player._restart_current_track(ended_track)

        backend.play.assert_not_awaited()
