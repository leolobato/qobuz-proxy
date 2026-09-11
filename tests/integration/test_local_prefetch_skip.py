"""Regression for #26 through the real player and local audio backend."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import numpy as np
import pytest

from qobuz_proxy.backends.local.backend import LocalAudioBackend
from qobuz_proxy.backends.types import PlaybackState
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QueueTrack


async def _wait_until(condition):
    async def wait():
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.parametrize("command", ["set_state", "play_track", "next_track"])
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize(
    "stage", ["downloaded", "downloading", "decoding", "decoded", "format_change"]
)
async def test_manual_skip_reuses_prefetched_download(command, stage, paused):
    backend = LocalAudioBackend()
    backend._stream = MagicMock()
    download_gate = asyncio.Event()
    decode_gate = asyncio.Event()
    if stage != "downloading":
        download_gate.set()
    if stage != "decoding":
        decode_gate.set()
    next_rate = 96000 if stage == "format_change" else 44100
    # A short outgoing track lets natural advance start decoding before the skip.
    frames = 1000 if stage in ("decoding", "decoded", "format_change") else 44100 * 20
    old_audio = np.zeros((frames, 2), dtype=np.float32)
    next_audio = np.ones((next_rate * 20, 2), dtype=np.float32)

    async def download(url):
        if url.endswith("/2"):
            await download_gate.wait()
        return url.encode()

    async def decode(data):
        if data.endswith(b"/2"):
            await decode_gate.wait()
            return next_audio, next_rate
        return old_audio, 44100

    backend._download = AsyncMock(side_effect=download)
    backend._decode = AsyncMock(side_effect=decode)
    metadata = MagicMock()
    metadata.get_streaming_url = AsyncMock(side_effect=lambda track_id: f"http://test/{track_id}")
    meta = MagicMock()
    meta.to_dict.return_value = {"duration_ms": 20000}
    metadata.get_metadata = AsyncMock(return_value=meta)
    metadata.get_track_format.side_effect = lambda track_id: (
        6,
        next_rate if track_id == "2" else 44100,
        16,
    )
    queue = MagicMock()
    queue.advance_to_next = AsyncMock(return_value=QueueTrack(queue_item_id=2, track_id="2"))
    player = QobuzPlayer(queue=queue, metadata_service=metadata, backend=backend)
    player.set_next_track_callbacks(lambda: {"trackId": "2", "queueItemId": 2}, lambda: None)

    try:
        await player.play_track(1, "1")
        await player._prepare_next_track_for_gapless()
        if stage == "downloading":
            await _wait_until(lambda: backend._download.await_count == 2)
        else:
            await backend._next_prefetch_task
        if stage == "decoding":
            await _wait_until(lambda: backend._decode.await_count == 2)
        elif stage == "decoded":
            await _wait_until(lambda: backend._transition_pending)
        elif stage == "format_change":
            await _wait_until(lambda: backend._next_prefetch_task is None)
        if paused:
            await player.pause()
        # A refreshed signed URL still refers to the same track and quality.
        metadata.get_streaming_url = AsyncMock(return_value="http://test/2?renewed")
        old_buffer = backend._ring_buffer
        started = MagicMock()
        backend.on_next_track_started(started)

        if command == "set_state":
            skip = player.apply_remote_state(
                track_id="2", queue_item_id=2, position_ms=0, playing_state=2
            )
        elif command == "play_track":
            skip = player.play_track(2, "2")
        else:
            skip = player.next_track()
        skip_task = asyncio.create_task(skip)
        try:
            await _wait_until(lambda: backend._stream.stop.called)
            download_gate.set()
            decode_gate.set()
            await asyncio.wait_for(skip_task, timeout=2)
        finally:
            if not skip_task.done():
                skip_task.cancel()
                await asyncio.gather(skip_task, return_exceptions=True)

        assert backend._download.await_args_list == [call("http://test/1"), call("http://test/2")]
        assert backend._decode.await_count == 2
        assert backend._audio_data is next_audio
        assert player.current_track.track_id == "2"
        assert player._state == PlaybackState.PLAYING
        assert await backend.get_position() == 0
        assert not player._gapless_armed
        assert backend._next_prefetch_task is None
        assert backend._next_decode_task is None
        assert old_buffer.available() == 0
        await _wait_until(lambda: backend._ring_buffer.available() > 0)
        np.testing.assert_array_equal(backend._ring_buffer.read(100), next_audio[:100])
        backend._stream.open.assert_called_with(next_rate, 2)
        started.assert_not_called()
    finally:
        await backend.stop()
