"""Tests that playback commands are serialized and superseded (latest-wins).

A track switch in the Qobuz app sends a burst of SET_STATE messages, which used
to fire overlapping load/play/stop calls and concurrent SOAP requests that wedge
DLNA renderers. The player now serializes commands behind a lock and lets a newer
command supersede an older one waiting on it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from qobuz_proxy.playback.player import QobuzPlayer


class ConcurrencyTrackingBackend(AudioBackend):
    """Backend that records overlap and the order of played tracks."""

    def __init__(self) -> None:
        super().__init__(name="test")
        self.active = 0
        self.max_active = 0
        self.played: list[str] = []

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # Yield so any concurrent play() would be observed as overlap.
            await asyncio.sleep(0.01)
            self.played.append(metadata.track_id)
        finally:
            self.active -= 1

    async def pause(self) -> None: ...
    async def resume(self) -> bool:
        return True

    async def stop(self) -> None: ...
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


def _make_player() -> tuple[QobuzPlayer, ConcurrencyTrackingBackend]:
    backend = ConcurrencyTrackingBackend()

    metadata = MagicMock()
    metadata.get_streaming_url = MagicMock(
        side_effect=lambda track_id: _coro(f"http://test/{track_id}")
    )
    metadata.get_metadata = MagicMock(side_effect=lambda track_id: _coro(None))
    metadata.get_track_actual_quality = MagicMock(return_value=None)
    # (actual_quality, sample_rate, bit_depth); 0s = cache miss, fall back to max quality.
    metadata.get_track_format = MagicMock(return_value=(0, 0, 0))
    metadata.log_now_playing_info = MagicMock()

    queue = MagicMock()
    queue.set_current_by_item_id = AsyncMock(return_value=True)
    player = QobuzPlayer(queue=queue, metadata_service=metadata, backend=backend)
    return player, backend


async def _coro(value):  # type: ignore[no-untyped-def]
    return value


class TestPositionedStart:
    async def test_positioned_start_reports_start_position(self) -> None:
        """Regression for BUG-18: starting at a position must not report 0:00
        to the app (progress bar snapped to zero until the next heartbeat)."""
        player, backend = _make_player()

        await player.play_track(queue_item_id=1, track_id="42", position_ms=60_000)

        assert player._position_value_ms == 60_000
        assert player.current_position_ms >= 60_000


class TestPlaybackSerialization:
    async def test_concurrent_play_track_never_overlaps(self) -> None:
        player, backend = _make_player()

        track_ids = [f"{i}" for i in range(1, 6)]
        await asyncio.gather(
            *(player.play_track(queue_item_id=i, track_id=t) for i, t in enumerate(track_ids))
        )

        # The critical invariant: backend.play never ran concurrently.
        assert backend.max_active == 1
        # Latest-wins: the final state is the last requested track.
        assert player.current_track is not None
        assert player.current_track.track_id == track_ids[-1]
        assert backend.played[-1] == track_ids[-1]
        # Supersede dropped at least one intermediate request.
        assert len(backend.played) < len(track_ids)

    async def test_generation_supersede_skips_stale_command(self) -> None:
        """A command whose generation is bumped before it acquires the lock is skipped."""
        player, backend = _make_player()

        # Hold the lock, then queue a command and bump the generation behind it.
        await player._playback_lock.acquire()
        stale = asyncio.create_task(player.play_track(queue_item_id=0, track_id="stale"))
        await asyncio.sleep(0)  # let `stale` reach the lock and register its generation
        player._next_generation()  # a newer command supersedes `stale`
        player._playback_lock.release()

        result = await stale
        assert result is False
        assert backend.played == []


class TestApplyRemoteStateSerialization:
    """A SET_STATE is load+seek+play applied as one atomic unit (apply_remote_state).

    These cover the residual race from the PR review: overlapping SET_STATE
    sequences must never interleave their load/play steps, so the newest one
    always wins as a whole and playback never ends up on a stale track.
    """

    async def test_concurrent_apply_remote_state_never_overlaps(self) -> None:
        player, backend = _make_player()

        track_ids = [str(i) for i in range(1, 6)]
        await asyncio.gather(
            *(
                player.apply_remote_state(
                    track_id=t, queue_item_id=i, position_ms=0, playing_state=2
                )
                for i, t in enumerate(track_ids)
            )
        )

        # No interleaving of the load/play steps across SET_STATE sequences.
        assert backend.max_active == 1
        # Newest SET_STATE wins as a unit — never left on a stale track.
        assert player.current_track is not None
        assert player.current_track.track_id == track_ids[-1]
        assert backend.played[-1] == track_ids[-1]

    async def test_newer_set_state_wins_when_queued_behind_older(self) -> None:
        """Reproduce the reviewer's interleave: older sequence is in-flight, a newer
        one is queued behind the lock, and a third (newest) supersedes the queued one.
        The final track must be the newest, and the superseded one must not run."""
        player, backend = _make_player()

        # Older SET_STATE (track A) grabs the lock first and is mid-flight.
        older = asyncio.create_task(
            player.apply_remote_state(track_id="A", queue_item_id=1, position_ms=0, playing_state=2)
        )
        await asyncio.sleep(0)  # let A acquire the lock and start loading/playing

        # A newer SET_STATE (track B) queues behind the lock...
        newer = asyncio.create_task(
            player.apply_remote_state(track_id="B", queue_item_id=2, position_ms=0, playing_state=2)
        )
        await asyncio.sleep(0)  # let B register its generation, then wait on the lock
        # ...and an even newer SET_STATE (track C) supersedes B before B runs.
        newest = asyncio.create_task(
            player.apply_remote_state(track_id="C", queue_item_id=3, position_ms=0, playing_state=2)
        )

        await asyncio.gather(older, newer, newest)

        assert backend.max_active == 1
        # B was superseded by C and must never have played.
        assert "B" not in backend.played
        # Final state is the newest request (C).
        assert player.current_track is not None
        assert player.current_track.track_id == "C"
        assert backend.played[-1] == "C"


class TestStopDuringLoad:
    async def test_stop_queued_during_load_prevents_backend_play(self) -> None:
        """A stop that arrives while SET_STATE is still fetching the track URL
        (the server deactivating a renderer ~10 ms after its join snapshot)
        must win: the track never reaches the backend."""
        player, backend = _make_player()

        async def slow_url(track_id: str) -> str:
            await asyncio.sleep(0.05)
            return f"http://test/{track_id}"

        player.metadata.get_streaming_url = MagicMock(side_effect=slow_url)

        apply = asyncio.create_task(
            player.apply_remote_state(
                track_id="7",
                queue_item_id=1,
                position_ms=0,
                playing_state=2,
                context_uuid=None,
            )
        )
        await asyncio.sleep(0.01)  # load in progress, holding the lock
        await player.stop_playback()  # queues behind the lock, bumps generation
        await apply

        assert backend.played == []
        assert player.state == PlaybackState.STOPPED
