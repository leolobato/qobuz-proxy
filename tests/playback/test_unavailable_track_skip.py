"""Skipping tracks Qobuz cannot serve ("Not available" in the user's region).

Regression tests for GitHub #21 (album stops at an unavailable track) and #22
(a track change was reported as STOPPED while loading, so the app paused the
freshly skipped-to track).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends import PlaybackState
from qobuz_proxy.playback import player as player_module
from qobuz_proxy.playback.queue import RepeatMode

from tests.playback.test_player_serialization import _coro, _make_player

UNAVAILABLE = "500"
UNAVAILABLE_2 = "501"


def _setup(unavailable=(UNAVAILABLE, UNAVAILABLE_2)):
    """Player whose metadata service has no URL for the unavailable tracks, plus a
    stand-in for the command handler's stored nextQueueItem."""
    player, backend = _make_player()
    player.metadata.get_streaming_url = MagicMock(
        side_effect=lambda tid: _coro(None if tid in unavailable else f"http://t/{tid}")
    )
    queue_state = MagicMock()
    queue_state.repeat_mode = RepeatMode.OFF
    player.queue.get_state = AsyncMock(return_value=queue_state)
    store: dict = {"next": None}
    player.set_next_track_callbacks(
        get_callback=lambda: store["next"],
        clear_callback=lambda: store.__setitem__("next", None),
    )
    return player, backend, store


async def _play_then_end_into(player, store, next_track_id, next_item_id):
    """Play track 1, then have it end naturally with the given next item stored."""
    assert await player.play_track(queue_item_id=1, track_id="1")
    store["next"] = {"trackId": next_track_id, "queueItemId": next_item_id}
    await player._handle_track_ended(player.current_track)


class TestAutoAdvanceIntoUnavailableTrack:
    async def test_reports_unavailable_item_as_loading_and_waits(self) -> None:
        player, backend, store = _setup()

        await _play_then_end_into(player, store, UNAVAILABLE, 5)

        assert backend.played == ["1"]
        assert player.state == PlaybackState.LOADING
        assert player.current_track is not None
        assert player.current_track.track_id == UNAVAILABLE
        assert player.current_track.queue_item_id == 5
        assert player._skip_pending_track is player.current_track
        assert player._skip_timeout_task is not None
        assert player.current_position_ms == 0
        assert player.duration_ms == 0
        player._clear_skip_pending()

    async def test_skips_to_next_item_named_by_server(self) -> None:
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)

        # Server answers our report with SET_STATE(current=5, next=6)
        store["next"] = {"trackId": "6", "queueItemId": 6}
        await player.apply_remote_state(
            track_id=UNAVAILABLE, queue_item_id=5, position_ms=0, playing_state=2
        )

        assert backend.played == ["1", "6"]
        assert player.state == PlaybackState.PLAYING
        assert player.current_track.track_id == "6"
        assert player._skip_pending_track is None
        assert player._skip_timeout_task is None
        # Consumed, so the gapless arm won't queue track 6 as its own successor
        assert store["next"] is None

    async def test_two_unavailable_tracks_in_a_row(self) -> None:
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)

        store["next"] = {"trackId": UNAVAILABLE_2, "queueItemId": 6}
        await player.apply_remote_state(
            track_id=UNAVAILABLE, queue_item_id=5, position_ms=0, playing_state=2
        )
        assert player.state == PlaybackState.LOADING
        assert player.current_track.track_id == UNAVAILABLE_2
        assert player._skip_pending_track is player.current_track

        store["next"] = {"trackId": "7", "queueItemId": 7}
        await player.apply_remote_state(
            track_id=UNAVAILABLE_2, queue_item_id=6, position_ms=0, playing_state=2
        )
        assert backend.played == ["1", "7"]
        assert player.state == PlaybackState.PLAYING

    async def test_stops_when_nothing_follows(self) -> None:
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)

        store["next"] = None
        await player.apply_remote_state(
            track_id=UNAVAILABLE, queue_item_id=5, position_ms=0, playing_state=2
        )

        assert player.state == PlaybackState.STOPPED
        assert player.current_track is None
        assert player._skip_pending_track is None
        assert player._skip_timeout_task is None

    async def test_stops_when_server_never_answers(self, monkeypatch) -> None:
        monkeypatch.setattr(player_module, "_UNAVAILABLE_SKIP_TIMEOUT_S", 0.05)
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)
        timeout_task = player._skip_timeout_task

        await asyncio.wait_for(timeout_task, timeout=1.0)

        assert player.state == PlaybackState.STOPPED
        assert player.current_track is None
        assert player._skip_pending_track is None

    async def test_user_track_change_cancels_the_wait(self) -> None:
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)
        timeout_task = player._skip_timeout_task

        await player.apply_remote_state(
            track_id="9", queue_item_id=9, position_ms=0, playing_state=2
        )

        assert backend.played == ["1", "9"]
        assert player.state == PlaybackState.PLAYING
        assert player._skip_pending_track is None
        await asyncio.sleep(0)
        assert timeout_task.cancelled()

    async def test_position_only_set_state_is_ignored_while_waiting(self) -> None:
        player, backend, store = _setup()
        await _play_then_end_into(player, store, UNAVAILABLE, 5)

        await player.apply_remote_state(
            track_id=None, queue_item_id=None, position_ms=1234, playing_state=2
        )

        assert player.state == PlaybackState.LOADING
        assert player._skip_pending_track is not None
        assert backend.played == ["1"]
        player._clear_skip_pending()

    async def test_backend_failure_is_not_treated_as_unavailable(self) -> None:
        """Only a track with no URL is skipped; a renderer error stays an error."""
        player, backend, store = _setup()

        async def failing_play(url, metadata):
            raise RuntimeError("renderer offline")

        assert await player.play_track(queue_item_id=1, track_id="1")
        backend.play = failing_play
        store["next"] = {"trackId": "8", "queueItemId": 8}
        await player._handle_track_ended(player.current_track)

        assert player.state == PlaybackState.ERROR
        assert player._skip_pending_track is None
        assert player._skip_timeout_task is None


class TestExplicitPlayOfUnavailableTrack:
    async def test_skips_straight_to_the_stored_next_item(self) -> None:
        """The SET_STATE carries nextQueueItem, so no round trip is needed."""
        player, backend, store = _setup()
        store["next"] = {"trackId": "7", "queueItemId": 7}

        await player.apply_remote_state(
            track_id=UNAVAILABLE, queue_item_id=5, position_ms=0, playing_state=2
        )

        assert backend.played == ["7"]
        assert player.state == PlaybackState.PLAYING
        assert store["next"] is None

    async def test_load_only_of_unavailable_track_does_not_skip(self) -> None:
        """A PAUSED-intent load (no play requested) leaves the decision to the app."""
        player, backend, store = _setup()
        store["next"] = {"trackId": "7", "queueItemId": 7}

        await player.apply_remote_state(
            track_id=UNAVAILABLE, queue_item_id=5, position_ms=0, playing_state=3
        )

        assert backend.played == []
        assert player.state == PlaybackState.STOPPED
        assert player._skip_pending_track is None


class TestTrackChangeNeverReportsStopped:
    async def test_states_reported_during_a_skip(self) -> None:
        """Regression for GitHub #22: on the local backend the whole file is
        downloaded before play, and a STOPPED report in that window made the
        app answer with PAUSED, stalling the skip."""
        player, backend = _make_player()
        assert await player.play_track(queue_item_id=1, track_id="1")

        reported: list[PlaybackState] = []

        async def record() -> None:
            reported.append(player.state)

        player.set_state_update_callback(record)  # type: ignore[arg-type]

        await player.apply_remote_state(
            track_id="2", queue_item_id=2, position_ms=0, playing_state=2
        )

        assert PlaybackState.STOPPED not in reported
        assert reported[0] == PlaybackState.LOADING
        assert reported[-1] == PlaybackState.PLAYING
        assert backend.played == ["1", "2"]

    async def test_loading_state_held_through_load_when_play_intended(self) -> None:
        player, backend = _make_player()
        assert await player.play_track(queue_item_id=1, track_id="1")
        seen: list[PlaybackState] = []
        real_get_url = player.metadata.get_streaming_url

        def spy(track_id):
            seen.append(player.state)
            return real_get_url(track_id)

        player.metadata.get_streaming_url = MagicMock(side_effect=spy)

        await player._play_track_locked(2, "2")

        # State observed while the URL was being fetched for the new track
        assert seen == [PlaybackState.LOADING]

    async def test_load_only_still_leaves_player_stopped(self) -> None:
        player, backend = _make_player()
        assert await player.play_track(queue_item_id=1, track_id="1")

        assert await player.load_track(queue_item_id=2, track_id="2")

        assert player.state == PlaybackState.STOPPED

    async def test_seek_is_ignored_while_loading(self) -> None:
        player, backend = _make_player()
        assert await player.play_track(queue_item_id=1, track_id="1")
        player._state = PlaybackState.LOADING
        player._current_duration_ms = 100_000
        backend.seek = AsyncMock()

        assert await player.seek(5000) is False
        backend.seek.assert_not_awaited()
