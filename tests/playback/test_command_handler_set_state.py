"""Integration tests for SET_STATE handling via PlaybackCommandHandler.

Covers the residual race called out in PR review: each SET_STATE message is
dispatched as its own task, so two overlapping SET_STATE sequences must not
interleave their load/seek/play steps. The handler now delegates the whole
sequence to player.apply_remote_state(), which applies it atomically.
"""

import asyncio

import pytest

from qobuz_proxy.backends import PlaybackState
from qobuz_proxy.playback import command_handler as ch
from qobuz_proxy.playback.command_handler import PlaybackCommandHandler
from qobuz_proxy.proto import qconnect_payload_pb2 as pb

from tests.playback.test_player_serialization import _make_player


def _set_state_msg(
    *,
    track_id: int,
    queue_item_id: int,
    playing_state: int | None = 2,
    position_ms: int | None = None,
    context_uuid: bytes | None = None,
):
    """Build a server->renderer SET_STATE (type 41) protobuf message."""
    msg = pb.QConnectMessage()
    msg.messageType = 41
    st = msg.srvrRndrSetState
    if playing_state is not None:
        st.playingState = playing_state
    if position_ms is not None:
        st.currentPosition = position_ms
    st.currentQueueItem.queueItemId = queue_item_id
    st.currentQueueItem.trackId = track_id
    if context_uuid is not None:
        st.currentQueueItem.contextUuid = context_uuid
    return msg


class TestSetStateHandling:
    async def test_set_state_syncs_queue_index(self) -> None:
        """Regression for BUG-33: the queue's current index must follow the
        currentQueueItem of SET_STATE, or queue-based fallbacks (auto-advance,
        get_current_track) act on a stale index."""
        from qobuz_proxy.playback.queue import QobuzQueue, QueueVersion

        player, backend = _make_player()
        queue = QobuzQueue()
        handler = PlaybackCommandHandler(player, queue=queue)

        await queue.load_queue(
            tracks=[
                {"queueItemId": 1, "trackId": 3001},
                {"queueItemId": 2, "trackId": 3002},
                {"queueItemId": 3, "trackId": 3003},
            ],
            version=QueueVersion(major=1, minor=0),
        )

        await handler._handle_set_state(_set_state_msg(track_id=3003, queue_item_id=3))

        state = await queue.get_state()
        assert state.current_queue_item_id == 3
        current = await queue.get_current_track()
        assert current is not None
        assert current.track_id == "3003"

    async def test_single_set_state_loads_and_plays(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=5))

        assert player.current_track is not None
        assert player.current_track.track_id == "2001"
        assert backend.played == ["2001"]
        assert player.state == PlaybackState.PLAYING

    async def test_set_state_propagates_context_uuid(self) -> None:
        """The currentQueueItem context UUID must reach the played track so
        the play report (listening history / scrobble) carries it."""
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        ctx = bytes(range(16))
        await handler._handle_set_state(
            _set_state_msg(track_id=2001, queue_item_id=5, context_uuid=ctx)
        )

        assert player.current_track is not None
        assert player.current_track.context_uuid == ctx

    async def test_next_item_context_preserved_on_contextless_resend(self) -> None:
        """A context-less resend of the same nextQueueItem must keep the context."""
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        ctx = bytes(range(16))

        first = _set_state_msg(track_id=1, queue_item_id=1)
        first.srvrRndrSetState.nextQueueItem.queueItemId = 2
        first.srvrRndrSetState.nextQueueItem.trackId = 1002
        first.srvrRndrSetState.nextQueueItem.contextUuid = ctx
        await handler._handle_set_state(first)
        assert handler.get_next_track_info()["contextUuid"] == ctx

        # Server resends the same next item without the optional contextUuid.
        second = _set_state_msg(track_id=1, queue_item_id=1)
        second.srvrRndrSetState.nextQueueItem.queueItemId = 2
        second.srvrRndrSetState.nextQueueItem.trackId = 1002
        await handler._handle_set_state(second)

        assert handler.get_next_track_info()["contextUuid"] == ctx

    async def test_overlapping_set_state_newest_wins(self) -> None:
        """Two SET_STATE messages handled concurrently (as independent tasks):
        their load/play steps must not interleave and the newer track must win —
        the exact path that previously left playback on a stale track."""
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        older = _set_state_msg(track_id=1001, queue_item_id=1)
        newer = _set_state_msg(track_id=1002, queue_item_id=2)

        await asyncio.gather(
            handler._handle_set_state(older),
            handler._handle_set_state(newer),
        )

        # No interleaving of load/play across the two SET_STATE sequences.
        assert backend.max_active == 1
        # The newer SET_STATE wins as a whole — never left on the stale older track.
        assert player.current_track is not None
        assert player.current_track.track_id == "1002"
        assert backend.played[-1] == "1002"


class TestNextTrackSentinel:
    """The app sends nextQueueItem with all-ones ids to mean "no next track"
    (issue #17: last track of an album with another album queued in a
    different context). It must be treated like an absent nextQueueItem."""

    async def test_sentinel_next_item_is_not_stored(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        msg = _set_state_msg(track_id=1001, queue_item_id=1)
        msg.srvrRndrSetState.nextQueueItem.queueItemId = 0xFFFFFFFFFFFFFFFF
        msg.srvrRndrSetState.nextQueueItem.trackId = 0xFFFFFFFF
        await handler._handle_set_state(msg)

        assert handler.get_next_track_info() is None

    async def test_sentinel_track_id_alone_is_not_stored(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        msg = _set_state_msg(track_id=1001, queue_item_id=1)
        msg.srvrRndrSetState.nextQueueItem.queueItemId = 2
        msg.srvrRndrSetState.nextQueueItem.trackId = 0xFFFFFFFF
        await handler._handle_set_state(msg)

        assert handler.get_next_track_info() is None

    async def test_sentinel_clears_previous_next_and_notifies(self) -> None:
        """A sentinel replacing a real next track must clear it and fire the
        change callback so a stale gapless arm is torn down."""
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        changed = 0

        async def on_changed() -> None:
            nonlocal changed
            changed += 1

        first = _set_state_msg(track_id=1001, queue_item_id=1)
        first.srvrRndrSetState.nextQueueItem.queueItemId = 2
        first.srvrRndrSetState.nextQueueItem.trackId = 1002
        await handler._handle_set_state(first)
        assert handler.get_next_track_info() is not None

        handler.set_on_next_track_changed(on_changed)
        second = _set_state_msg(track_id=1001, queue_item_id=1)
        second.srvrRndrSetState.nextQueueItem.queueItemId = 0xFFFFFFFFFFFFFFFF
        second.srvrRndrSetState.nextQueueItem.trackId = 0xFFFFFFFF
        await handler._handle_set_state(second)

        assert handler.get_next_track_info() is None
        assert changed == 1

    async def test_track_end_with_sentinel_stops_instead_of_advancing(self) -> None:
        """Issue #17 repro: track ends while the stored next is the sentinel —
        playback must stop cleanly, never try to load track 4294967295."""
        from qobuz_proxy.playback.queue import QobuzQueue

        player, backend = _make_player()
        player.queue = QobuzQueue()  # real queue: track-end path awaits get_state()
        handler = PlaybackCommandHandler(player, queue=player.queue)
        player.set_next_track_callbacks(
            get_callback=handler.get_next_track_info,
            clear_callback=handler.clear_next_track_info,
        )

        msg = _set_state_msg(track_id=1001, queue_item_id=1)
        msg.srvrRndrSetState.nextQueueItem.queueItemId = 0xFFFFFFFFFFFFFFFF
        msg.srvrRndrSetState.nextQueueItem.trackId = 0xFFFFFFFF
        await handler._handle_set_state(msg)

        await player._handle_track_ended(player.current_track)

        assert player.state == PlaybackState.STOPPED
        assert "4294967295" not in backend.played


def _set_active_msg(active: bool):
    """Build a server->renderer SET_ACTIVE (type 43) protobuf message."""
    msg = pb.QConnectMessage()
    msg.messageType = 43
    msg.srvrRndrSetActive.active = active
    return msg


class TestJoinSnapshotHold:
    """When a speaker (re)joins while another renderer owns the session, the
    server replays SET_ACTIVE(true) → SET_STATE(PLAYING) → SET_ACTIVE(false)
    within ~10 ms. An idle speaker must not start the other renderer's track."""

    @pytest.fixture(autouse=True)
    def _short_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ch, "JOIN_SNAPSHOT_GRACE_S", 0.05)

    async def test_snapshot_dropped_when_renderer_deactivated(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        handler.note_connected()

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))
        assert handler._held_snapshot is not None
        assert player.current_track is None

        await handler._handle_set_active(_set_active_msg(False))
        await asyncio.sleep(0.1)

        assert backend.played == []
        assert player.current_track is None
        assert player.state == PlaybackState.STOPPED
        assert handler._held_snapshot is None

    async def test_snapshot_applies_when_renderer_stays_active(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        handler.note_connected()

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))
        assert backend.played == []

        await asyncio.sleep(0.1)

        assert backend.played == ["2001"]
        assert player.state == PlaybackState.PLAYING
        assert handler._held_snapshot is None

    async def test_snapshot_not_held_while_playing(self) -> None:
        """The session owner's own reconnect snapshot must apply at once."""
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        await player.play_track(queue_item_id=1, track_id="2001")
        handler.note_connected()

        await handler._handle_set_state(_set_state_msg(track_id=2002, queue_item_id=2))

        assert handler._held_snapshot is None
        assert backend.played == ["2001", "2002"]

    async def test_paused_snapshot_not_held(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        handler.note_connected()

        await handler._handle_set_state(
            _set_state_msg(track_id=2001, queue_item_id=1, playing_state=3)
        )

        assert handler._held_snapshot is None
        assert player.current_track is not None
        assert player.current_track.track_id == "2001"
        assert backend.played == []

    async def test_set_state_outside_connect_window_applies_immediately(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        handler.note_connected()
        assert handler._connected_at is not None
        handler._connected_at -= ch.JOIN_SNAPSHOT_WINDOW_S + 1

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))

        assert handler._held_snapshot is None
        assert backend.played == ["2001"]

    async def test_no_connect_notice_means_no_hold(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))

        assert handler._held_snapshot is None
        assert backend.played == ["2001"]

    async def test_newer_set_state_supersedes_held_snapshot(self) -> None:
        player, backend = _make_player()
        handler = PlaybackCommandHandler(player)
        handler.note_connected()

        await handler._handle_set_state(_set_state_msg(track_id=2001, queue_item_id=1))
        first = handler._held_snapshot
        await handler._handle_set_state(_set_state_msg(track_id=2002, queue_item_id=2))
        assert first is not None
        await asyncio.sleep(0)
        assert first.cancelled()

        await asyncio.sleep(0.1)

        assert backend.played == ["2002"]
