"""Integration tests for SET_STATE handling via PlaybackCommandHandler.

Covers the residual race called out in PR review: each SET_STATE message is
dispatched as its own task, so two overlapping SET_STATE sequences must not
interleave their load/seek/play steps. The handler now delegates the whole
sequence to player.apply_remote_state(), which applies it atomically.
"""

import asyncio

from qobuz_proxy.backends import PlaybackState
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
