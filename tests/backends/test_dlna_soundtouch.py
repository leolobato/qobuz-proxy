"""Renderers that do not name our track in GetMediaInfo.CurrentURI.

GitHub #43: Bose SoundTouch reports an empty CurrentURI while it plays (or has
paused) our stream; only GetPositionInfo.TrackURI names it. Every transport
command was dropped because the source looked unknown. After a Stop it puts a
``qplay://`` placeholder in CurrentURI, which looked like another source taking
over, so a skip (Stop, then Play) left the Qobuz session.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

QOBUZ_URI = "http://proxy:7120/audio/123.flac"
NEXT_URI = "http://proxy:7120/audio/456_2.flac"
OTHER_URI = "http://elsewhere/x.mp3"
# What SoundTouch reports after a Stop.
PLACEHOLDER_URI = "qplay://"
INVALID_TRACK_URI = "http://invalidHost.com/invalidUri.mp3"


@pytest.fixture
def backend():
    backend = DLNABackend("192.0.2.1", name="SoundTouch")
    backend._current_proxy_url = QOBUZ_URI
    backend._state = PlaybackState.PLAYING
    backend._playback_started_at = 0.0  # well past the start grace period
    client = AsyncMock(spec=DLNAClient)
    client.get_media_info.return_value = ""
    client.get_track_uri.return_value = QOBUZ_URI
    client.get_transport_info.return_value = "PLAYING"
    client.get_position_info.return_value = 30_000
    client.pause.return_value = True
    client.play.return_value = True
    client.seek.return_value = True
    client.stop.return_value = True
    backend._client = client
    return backend


async def poll(backend, monkeypatch, times: int = 1) -> None:
    backend._is_connected = True
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > times:
            backend._is_connected = False

    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.asyncio.sleep", tick)
    await backend._poll_state_loop()


@pytest.mark.parametrize("track_uri", [QOBUZ_URI, NEXT_URI])
async def test_empty_current_uri_with_our_track_uri_is_controllable(backend, track_uri):
    backend._next_track_proxy_url = NEXT_URI
    backend._client.get_track_uri.return_value = track_uri
    await backend.pause()
    assert await backend.resume()
    await backend.seek(4000)
    await backend.stop()
    backend._client.pause.assert_awaited_once()
    backend._client.play.assert_awaited_once()
    backend._client.seek.assert_awaited_once_with(4000)
    backend._client.stop.assert_awaited_once()
    assert not backend._external_playback


async def test_empty_current_uri_with_unrelated_track_uri_is_not_ours(backend):
    """Nothing names our track: fail closed, as for any empty CurrentURI."""
    backend._client.get_track_uri.return_value = OTHER_URI
    await backend.pause()
    with pytest.raises(RuntimeError, match="Cannot seek"):
        await backend.seek(4000)
    backend._client.pause.assert_not_awaited()
    backend._client.seek.assert_not_awaited()
    assert not backend._external_playback


async def test_failed_current_uri_read_still_fails_closed(backend):
    backend._client.get_media_info.return_value = None
    await backend.pause()
    backend._client.pause.assert_not_awaited()
    backend._client.get_track_uri.assert_not_awaited()
    assert not backend._external_playback


async def test_gapless_move_is_seen_through_track_uri(backend, monkeypatch):
    backend._next_track_proxy_url = NEXT_URI
    backend._next_track_metadata = MagicMock(duration_ms=180_000)
    backend._client.get_track_uri.return_value = NEXT_URI
    started = MagicMock()
    backend.on_next_track_started(started)
    await poll(backend, monkeypatch)
    started.assert_called_once()
    assert backend._current_proxy_url == NEXT_URI
    assert backend._next_track_proxy_url is None


async def stop_and_show_placeholder(backend, transport: str | None = "PAUSED_PLAYBACK") -> None:
    """Our Stop goes through, then the renderer shows its idle placeholder."""
    await backend.stop()
    backend._client.stop.assert_awaited_once()
    backend._client.get_media_info.return_value = PLACEHOLDER_URI
    backend._client.get_track_uri.return_value = INVALID_TRACK_URI
    backend._client.get_transport_info.return_value = transport


async def test_skip_after_our_stop_loads_the_next_track(backend):
    await stop_and_show_placeholder(backend)
    backend._client.set_av_transport_uri.return_value = True
    metadata = BackendTrackMetadata(track_id="456", title="Next", artist="Artist")
    await backend.play(NEXT_URI, metadata)
    backend._client.set_av_transport_uri.assert_awaited_once()
    assert backend._current_proxy_url == NEXT_URI
    assert not backend._external_playback


async def test_app_may_load_onto_renderer_idle_after_our_stop(backend):
    takeover = AsyncMock()
    backend.on_external_playback(takeover)
    await stop_and_show_placeholder(backend)
    assert await backend.check_external_playback() is False
    assert await backend.can_apply_remote_state() is True
    takeover.assert_not_awaited()


async def test_placeholder_at_natural_end_with_our_track_uri_is_ours(backend):
    backend._client.get_media_info.return_value = PLACEHOLDER_URI
    backend._client.get_track_uri.return_value = QOBUZ_URI
    backend._client.get_transport_info.return_value = "STOPPED"
    assert await backend.resume()
    backend._client.play.assert_awaited_once()
    assert not backend._external_playback


@pytest.mark.parametrize("transport", ["PAUSED_PLAYBACK", "STOPPED"])
async def test_placeholder_without_our_stop_is_still_a_takeover(backend, transport):
    backend._client.get_media_info.return_value = PLACEHOLDER_URI
    backend._client.get_track_uri.return_value = OTHER_URI
    backend._client.get_transport_info.return_value = transport
    assert await backend.check_external_playback() is True


@pytest.mark.parametrize("transport", ["PLAYING", "TRANSITIONING"])
async def test_placeholder_while_playing_after_our_stop_is_a_takeover(backend, transport):
    await stop_and_show_placeholder(backend, transport)
    assert await backend.check_external_playback() is True
    assert await backend.can_apply_remote_state() is False


async def test_placeholder_after_our_stop_with_failed_state_read_fails_closed(backend):
    await stop_and_show_placeholder(backend, None)
    assert await backend.check_external_playback() is False
    assert await backend.can_apply_remote_state() is False
