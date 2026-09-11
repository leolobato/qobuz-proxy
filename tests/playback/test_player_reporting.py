"""The player must drive PlayReporter across its playback lifecycle.

Wires a real PlayReporter (over a mocked API client) into the player and checks
that play/pause/stop/track-end/track-switch produce the right report calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.playback.play_reporter import PlayReporter
from qobuz_proxy.playback.player import QobuzPlayer


class _Backend(AudioBackend):
    def __init__(self) -> None:
        super().__init__(name="test")

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> bool:
        return True

    async def stop(self, *, next_track_id: str | None = None) -> None: ...
    async def seek(self, position_ms: int) -> None: ...
    async def get_position(self) -> int:
        return 0

    async def set_volume(self, level: int) -> None: ...
    async def get_volume(self) -> int:
        return 0

    async def get_state(self) -> PlaybackState:
        return self._state

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...


async def _coro(value):  # type: ignore[no-untyped-def]
    return value


def _make_player_with_reporter():
    backend = _Backend()

    metadata = MagicMock()
    metadata.get_streaming_url = MagicMock(side_effect=lambda tid: _coro(f"http://t/{tid}"))
    metadata.get_metadata = MagicMock(side_effect=lambda tid: _coro(None))
    metadata.get_track_actual_quality = MagicMock(return_value=27)
    metadata.get_track_blob = MagicMock(return_value="theblob")
    metadata.get_track_format = MagicMock(return_value=(27, 96000, 24))
    metadata.log_now_playing_info = MagicMock()

    api = AsyncMock()
    api.report_streaming_start = AsyncMock(return_value=True)
    api.report_streaming_end = AsyncMock(return_value=True)
    reporter = PlayReporter(api)

    queue = MagicMock()
    player = QobuzPlayer(
        queue=queue, metadata_service=metadata, backend=backend, play_reporter=reporter
    )
    return player, api


class TestPlayerReporting:
    async def test_play_track_reports_start(self) -> None:
        player, api = _make_player_with_reporter()

        await player.play_track(queue_item_id=1, track_id="555")

        api.report_streaming_start.assert_awaited_once_with(track_id="555", format_id=27)

    async def test_stop_reports_end(self) -> None:
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="555")

        await player.stop_playback()

        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "555"
        assert api.report_streaming_end.await_args.kwargs["blob"] == "theblob"

    async def test_pause_does_not_report_end(self) -> None:
        """Pausing keeps the listen open — a streaming-end on pause would
        produce a premature/duplicate scrobble."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="555")

        await player.pause()

        api.report_streaming_end.assert_not_awaited()

    async def test_pause_resume_cycles_report_one_start_one_end(self) -> None:
        """A single listen with several pause/resume cycles reports exactly one
        start and, on stop, exactly one end (no duplicate scrobbles)."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="555")

        for _ in range(3):
            await player.pause()
            await player.play()  # resume

        api.report_streaming_end.assert_not_awaited()
        assert api.report_streaming_start.await_count == 1

        await player.stop_playback()

        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "555"

    async def test_load_only_track_change_while_paused_ends_previous(self) -> None:
        """A track change that only loads (no immediate play) while paused must
        still end the previous play — pause no longer closes the session."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()
        api.report_streaming_end.assert_not_awaited()

        # Load-only change to a new track (no playingState -> no play).
        await player.apply_remote_state(
            track_id="200", queue_item_id=2, position_ms=None, playing_state=None
        )

        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "100"

    async def test_external_pause_stops_reporter_clock(self) -> None:
        """A device-originated pause (detected by the monitor) must pause the
        played-time clock too, not just an app-driven pause."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")

        # The renderer reports it paused on its own.
        player.backend.get_state = AsyncMock(return_value=PlaybackState.PAUSED)
        player.backend.get_position = AsyncMock(return_value=0)

        player._is_running = True
        task = asyncio.create_task(player._playback_monitor_loop())
        await asyncio.sleep(0.6)  # allow one poll cycle (loop sleeps 0.5s)
        player._is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert player.state == PlaybackState.PAUSED
        api.report_streaming_end.assert_not_awaited()
        # Session stays open but its played-time clock is paused.
        assert player._play_reporter._active is not None
        assert player._play_reporter._active.segment_started_ms is None

    async def test_reload_while_paused_ends_session_so_next_play_is_fresh(self) -> None:
        """A quality reload while paused must end the play, so the next play of
        the same track reports a fresh start (new quality/blob), not a resume."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()

        await player.reload_current_track()
        api.report_streaming_end.assert_awaited_once()

        await player.play()  # plays the reloaded track from STOPPED

        # A brand-new start was reported (not merged into the old session).
        assert api.report_streaming_start.await_count == 2

    async def _run_monitor_briefly(self, player) -> None:
        player._is_running = True
        task = asyncio.create_task(player._playback_monitor_loop())
        await asyncio.sleep(0.6)  # allow one poll cycle (loop sleeps 0.5s)
        player._is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_external_stop_while_paused_closes_report_after_confirmation(self) -> None:
        """An external renderer stop (confirmed by consecutive polls) after an
        app pause must close the open play-report session."""
        from qobuz_proxy.playback.player import _PAUSED_STOP_CONFIRMATIONS

        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()

        # The renderer is stopped externally; one more poll crosses the
        # confirmation threshold.
        player.backend.get_state = AsyncMock(return_value=PlaybackState.STOPPED)
        player._paused_stop_polls = _PAUSED_STOP_CONFIRMATIONS - 1
        player._position_value_ms = 60_000  # paused mid-track

        await self._run_monitor_briefly(player)

        assert player.state == PlaybackState.STOPPED
        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "100"
        # BUG-19: the stale pause-point must be cleared, or a later "previous"
        # command takes the restart-seek branch on a stopped renderer (a no-op).
        assert player.current_position_ms == 0

    async def test_single_transient_stop_while_paused_does_not_close(self) -> None:
        """A single STOPPED reading (e.g. a transient SOAP failure, which
        get_state collapses to STOPPED) must not end a normal paused listen."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()

        player.backend.get_state = AsyncMock(return_value=PlaybackState.STOPPED)

        await self._run_monitor_briefly(player)  # one poll only

        assert player.state == PlaybackState.PAUSED
        api.report_streaming_end.assert_not_awaited()

    async def test_switching_track_reports_end_then_start(self) -> None:
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.play_track(queue_item_id=2, track_id="200")

        # First track ended, second track started.
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "100"
        assert api.report_streaming_start.await_count == 2
        assert api.report_streaming_start.await_args.kwargs["track_id"] == "200"

    async def test_adopting_track_midplay_does_not_report_start(self) -> None:
        """Handoff from the app: it already reported the play (we start at a
        non-zero position), so we must not report a duplicate start."""
        player, api = _make_player_with_reporter()

        # App played this to ~26s, then transfers to us.
        await player.apply_remote_state(
            track_id="900", queue_item_id=1, position_ms=26000, playing_state=2
        )

        api.report_streaming_start.assert_not_awaited()

    async def test_fresh_play_from_zero_reports_start(self) -> None:
        """A track we start ourselves (position ~0) is ours to report."""
        player, api = _make_player_with_reporter()

        await player.apply_remote_state(
            track_id="900", queue_item_id=1, position_ms=0, playing_state=2
        )

        api.report_streaming_start.assert_awaited_once_with(track_id="900", format_id=27)

    async def test_restart_paused_track_reports_fresh_play_on_resume(self) -> None:
        """Restarting a paused track (previous past threshold) ends the prior
        listen; the resumed replay reports as a fresh play, not a merge."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()
        player._position_value_ms = 10_000  # past the restart threshold

        await player.previous_track()  # restart current track
        api.report_streaming_end.assert_awaited_once()

        await player.play()  # resume -> fresh play of the restarted track
        assert api.report_streaming_start.await_count == 2

    async def test_same_track_new_queue_item_reports_fresh_play(self) -> None:
        """Replaying the same track from a different queue slot while paused must
        report a fresh play (keyed by queue item), not merge into the old one."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()
        api.report_streaming_end.assert_not_awaited()

        # Same track, different queue item, position 0, resume playing.
        await player.apply_remote_state(
            track_id="100", queue_item_id=2, position_ms=0, playing_state=2
        )

        # Old play ended and a fresh start reported for the new queue occurrence.
        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_start.await_count == 2

    async def test_late_queue_item_id_does_not_split_play(self) -> None:
        """A queue item id that arrives after the play started (initial id 0)
        is a late-fill, not a new occurrence — it must not split the listen."""
        player, api = _make_player_with_reporter()
        # First SET_STATE lacks a real queue item (proto default 0).
        await player.apply_remote_state(
            track_id="100", queue_item_id=0, position_ms=0, playing_state=2
        )
        assert api.report_streaming_start.await_count == 1

        # Later SET_STATE supplies the real queue item id, same track, playing.
        await player.apply_remote_state(
            track_id="100", queue_item_id=5, position_ms=0, playing_state=2
        )

        # No split: the same listen continues, no extra end/start.
        api.report_streaming_end.assert_not_awaited()
        assert api.report_streaming_start.await_count == 1
        assert player.current_track.queue_item_id == 5

    async def test_same_track_new_queue_item_while_playing_keeps_one_listen(self) -> None:
        """A queue-item change to the same track while PLAYING (continuous audio,
        e.g. a queue reorder reassigned the id) must NOT split the listen — that
        would double-scrobble. It adopts the id and stays one play."""
        player, api = _make_player_with_reporter()
        await player.play_track(queue_item_id=1, track_id="100")
        assert api.report_streaming_start.await_count == 1

        # Same track, new queue item, still playing uninterrupted.
        await player.apply_remote_state(
            track_id="100", queue_item_id=2, position_ms=0, playing_state=2
        )

        # One continuous listen: no extra end/start, but the id is adopted.
        api.report_streaming_end.assert_not_awaited()
        assert api.report_streaming_start.await_count == 1
        assert player.current_track.queue_item_id == 2

    async def test_shutdown_while_paused_reports_end(self) -> None:
        """Shutting down mid-listen (paused) must still close the play report."""
        player, api = _make_player_with_reporter()
        player.queue.stop = AsyncMock()
        await player.play_track(queue_item_id=1, track_id="100")
        await player.pause()
        api.report_streaming_end.assert_not_awaited()

        await player.stop()

        api.report_streaming_end.assert_awaited_once()
        assert api.report_streaming_end.await_args.kwargs["track_id"] == "100"

    async def test_no_reporter_is_safe(self) -> None:
        """A player built without a reporter must not crash on playback."""
        backend = _Backend()
        metadata = MagicMock()
        metadata.get_streaming_url = MagicMock(side_effect=lambda tid: _coro(f"http://t/{tid}"))
        metadata.get_metadata = MagicMock(side_effect=lambda tid: _coro(None))
        metadata.get_track_format = MagicMock(return_value=(0, 0, 0))
        metadata.get_track_actual_quality = MagicMock(return_value=None)
        metadata.log_now_playing_info = MagicMock()
        player = QobuzPlayer(queue=MagicMock(), metadata_service=metadata, backend=backend)

        await player.play_track(queue_item_id=1, track_id="555")
        await player.stop_playback()

        assert player.state == PlaybackState.STOPPED
