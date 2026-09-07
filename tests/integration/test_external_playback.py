"""A Qobuz reconnect must leave another controller's audio and queue untouched."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState
from qobuz_proxy.config import Config
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.ws_manager import WsManager
from qobuz_proxy.playback.command_handler import PlaybackCommandHandler
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QueueTrack
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker
from tests.connect.test_ws_manager import _last_join, _send_report
from tests.connect.test_ws_manager import valid_tokens as valid_tokens


QOBUZ_URI = "http://proxy:7120/audio/123.flac"
NEXT_URI = "http://proxy:7120/audio/456_2.flac"
SPOTIFY_URI = "x-sonos-spotify:spotify%3atrack%3aexample"


@pytest.fixture
def rig(valid_tokens):
    backend = DLNABackend("192.0.2.1", name="Kitchen")
    backend._is_sonos = True
    backend._current_proxy_url = QOBUZ_URI
    backend._next_track_proxy_url = NEXT_URI
    backend._next_track_queue_nr = 2
    backend._state = PlaybackState.PAUSED
    client = AsyncMock(spec=DLNAClient)
    client.get_track_uri.return_value = SPOTIFY_URI
    client.get_transport_info.return_value = "PLAYING"
    client.get_position_info.return_value = 60000
    backend._client = client

    metadata = MagicMock()
    metadata.get_streaming_url = AsyncMock(return_value=QOBUZ_URI)
    metadata.get_metadata = AsyncMock(return_value=None)
    metadata.get_track_format.return_value = (6, 44100, 16)
    queue = MagicMock()
    queue.set_current_by_item_id = AsyncMock()
    player = QobuzPlayer(queue, metadata, backend)
    player._current_track = QueueTrack(
        track_id="123", queue_item_id=1, streaming_url=QOBUZ_URI, duration_ms=116000
    )
    player._current_duration_ms = 116000
    player._state = PlaybackState.PAUSED
    player._set_position(4000)
    manager = WsManager(Config())
    manager.set_tokens(valid_tokens, activate=True)
    manager._session_uuid = b"s" * 16
    manager._ws = AsyncMock()
    manager._is_connected = True
    manager._renderer_active = True
    manager._active_confirmed = True
    handler = PlaybackCommandHandler(player)
    handler._note_active(True)
    manager.on_disconnected(handler.note_disconnected)
    tasks = []

    def dispatch(kind, message):
        tasks.append(handler.dispatch_message(kind, message))

    for kind in handler.get_message_types():
        manager.register_handler(kind, dispatch)

    speaker = Speaker.__new__(Speaker)
    speaker._ws_manager = manager
    speaker._player = player
    speaker._backend = backend
    backend.on_external_playback(speaker._on_external_playback)
    player.set_playback_permission_check(backend.can_apply_remote_state)
    player.set_next_track_callbacks(handler.get_next_track_info, handler.clear_next_track_info)
    return backend, client, player, manager, tasks


async def deliver_snapshot(manager, tasks, playing_state=3):
    batch = pb.QConnectBatch()
    batch.messages.add(messageType=43).srvrRndrSetActive.active = True
    state = batch.messages.add(messageType=41).srvrRndrSetState
    state.currentQueueItem.trackId = 123
    state.currentQueueItem.queueItemId = 1
    state.currentPosition = 4000
    state.playingState = playing_state
    await manager._handle_payload(
        DecodedMessage(msg_type=MessageType.PAYLOAD, payload=batch.SerializeToString())
    )
    await asyncio.gather(*tasks)
    tasks.clear()


def assert_no_transport_writes(client):
    for method in (
        "seek",
        "play",
        "pause",
        "stop",
        "set_av_transport_uri",
        "clear_queue",
        "add_uri_to_queue",
        "remove_track_from_queue",
        "set_next_av_transport_uri",
        "set_volume",
    ):
        getattr(client, method).assert_not_awaited()


@pytest.mark.parametrize("playing_state", [1, 2, 3])
async def test_reconnect_snapshot_releases_spotify_without_transport_writes(rig, playing_state):
    backend, client, player, manager, tasks = rig
    await deliver_snapshot(manager, tasks, playing_state)
    assert_no_transport_writes(client)
    assert player.state == PlaybackState.STOPPED
    assert backend._next_track_queue_nr is None
    assert not manager.is_renderer_active
    assert await _send_report(manager) is False

    # Even a server that replays active=True cannot reacquire an external source.
    await manager._send_join_session()
    assert _last_join(manager).isActive is False
    assert _last_join(manager).reason == 2
    await deliver_snapshot(manager, tasks, 2)
    assert_no_transport_writes(client)
    assert not manager.is_renderer_active


@pytest.mark.parametrize("already_detected", [True, False])
async def test_explicit_reselection_can_start_same_qobuz_track(rig, valid_tokens, already_detected):
    backend, client, player, manager, tasks = rig
    if already_detected:
        await deliver_snapshot(manager, tasks)
    speaker = Speaker(config=MagicMock(), api_client=MagicMock(), app_id="test")
    speaker._backend = backend
    speaker._player = player
    speaker._queue = player.queue
    speaker._ws_manager = manager
    await speaker._setup_websocket(valid_tokens)
    await manager._send_join_session()
    assert _last_join(manager).isActive is True
    assert _last_join(manager).reason == 1

    async def start_queue(index):
        client.get_track_uri.return_value = QOBUZ_URI
        return True

    client.play_from_queue.side_effect = start_queue
    await deliver_snapshot(manager, tasks, 2)
    client.play_from_queue.assert_awaited_once_with(0)
    client.seek.assert_awaited_once_with(4000)
    assert player.state == PlaybackState.PLAYING
    assert manager.is_renderer_active


@pytest.mark.parametrize("uri", [QOBUZ_URI, NEXT_URI])
async def test_current_and_gapless_qobuz_tracks_remain_controllable(rig, uri):
    backend, client, _, manager, _ = rig
    client.get_track_uri.return_value = uri
    await backend.seek(4000)
    assert await backend.resume()
    await backend.pause()
    client.seek.assert_awaited_once_with(4000)
    client.play.assert_awaited_once()
    client.pause.assert_awaited_once()
    assert manager.is_renderer_active


@pytest.mark.parametrize("uri", [SPOTIFY_URI, None, ""])
async def test_transport_and_queue_commands_fail_closed_for_foreign_or_unknown_uri(rig, uri):
    backend, client, _, _, _ = rig
    client.get_track_uri.return_value = uri
    with pytest.raises(RuntimeError, match="Cannot seek"):
        await backend.seek(4000)
    await backend.pause()
    assert not await backend.resume()
    metadata = BackendTrackMetadata(track_id="456", title="Next", artist="Artist")
    assert not await backend.set_next_track(NEXT_URI, metadata)
    await backend.clear_next_track()
    await backend.stop()
    await backend.disconnect()
    assert_no_transport_writes(client)
    client.disconnect.assert_awaited_once()
    assert backend._external_playback is (uri == SPOTIFY_URI)


async def test_source_change_during_poll_does_not_advance_qobuz_queue(rig, monkeypatch):
    backend, client, player, manager, _ = rig
    player._state = PlaybackState.PLAYING
    backend._state = PlaybackState.PLAYING
    client.get_transport_info.return_value = "STOPPED"
    ended = MagicMock()
    backend.on_track_ended(ended)
    backend._is_connected = True
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            backend._is_connected = False

    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.asyncio.sleep", tick)
    await backend._poll_state_loop()
    ended.assert_not_called()
    assert_no_transport_writes(client)
    assert player.state == PlaybackState.STOPPED
    assert not manager.is_renderer_active


async def test_standard_dlna_uses_media_uri(rig):
    backend, client, _, _, _ = rig
    backend._is_sonos = False
    client.get_media_info.return_value = SPOTIFY_URI
    assert await backend.check_external_playback()
    client.get_media_info.assert_awaited_once()
    client.get_track_uri.assert_not_awaited()


@pytest.mark.parametrize("uri", [None, ""])
async def test_unknown_source_blocks_snapshot_without_relinquishing_session(rig, uri):
    backend, client, player, manager, tasks = rig
    client.get_track_uri.return_value = uri
    await deliver_snapshot(manager, tasks, 2)
    assert_no_transport_writes(client)
    assert not backend._external_playback
    assert manager.is_renderer_active

    # A transient read failure must not require a new device selection.
    client.get_track_uri.return_value = QOBUZ_URI
    await deliver_snapshot(manager, tasks, 3)
    client.seek.assert_awaited_once_with(4000)
    assert player.state == PlaybackState.PAUSED


async def test_pending_track_load_cannot_replace_new_external_source(rig):
    backend, client, _, _, _ = rig
    metadata = BackendTrackMetadata(track_id="456", title="Next", artist="Artist")
    with pytest.raises(RuntimeError, match="External source"):
        await backend.play(NEXT_URI, metadata)
    assert_no_transport_writes(client)


async def test_loading_old_uri_is_not_mistaken_for_external_takeover(rig):
    import time

    backend, client, _, manager, _ = rig
    backend._playback_started_at = time.monotonic()
    assert not await backend.check_external_playback()
    with pytest.raises(RuntimeError, match="Cannot seek"):
        await backend.seek(4000)
    assert_no_transport_writes(client)
    client.get_track_uri.return_value = QOBUZ_URI
    await backend.seek(4000)
    client.seek.assert_awaited_once_with(4000)
    assert manager.is_renderer_active


async def test_inflight_uri_read_cannot_revoke_a_new_transport_load(rig):
    backend, client, _, manager, _ = rig
    reading = asyncio.Event()
    finish = asyncio.Event()

    async def read_uri():
        reading.set()
        await finish.wait()
        return SPOTIFY_URI

    client.get_track_uri.side_effect = read_uri
    check = asyncio.create_task(backend.check_external_playback())
    await reading.wait()
    backend._starting_playback = True
    finish.set()
    assert not await check
    assert manager.is_renderer_active
