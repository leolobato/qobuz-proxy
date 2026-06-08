"""Tests that playback commands are serialized and superseded (latest-wins).

A track switch in the Qobuz app sends a burst of SET_STATE messages, which used
to fire overlapping load/play/stop calls and concurrent SOAP requests that wedge
DLNA renderers. The player now serializes commands behind a lock and lets a newer
command supersede an older one waiting on it.
"""

import asyncio
from unittest.mock import MagicMock

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
    async def resume(self) -> None: ...
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
    metadata.log_now_playing_info = MagicMock()

    queue = MagicMock()
    player = QobuzPlayer(queue=queue, metadata_service=metadata, backend=backend)
    return player, backend


async def _coro(value):  # type: ignore[no-untyped-def]
    return value


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
