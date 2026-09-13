"""Wi-Fi blips must not skip tracks; a dead renderer must reconnect."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.backend import DLNABackend, PLAYBACK_STOP_CONFIRMATIONS
from qobuz_proxy.backends.types import PlaybackState


QOBUZ_URI = "http://proxy/qobuz"


def _playing_backend() -> tuple[DLNABackend, AsyncMock]:
    backend = DLNABackend("192.0.2.1", name="Test")
    backend._is_connected = True
    backend._is_sonos = True
    backend._state = PlaybackState.PLAYING
    backend._current_proxy_url = QOBUZ_URI
    backend._playback_started_at = 0.0
    client = AsyncMock()
    client.get_track_uri = AsyncMock(return_value=QOBUZ_URI)
    client.get_transport_info = AsyncMock(return_value="PLAYING")
    client.get_position_info = AsyncMock(return_value=1000)
    client.reset_session = AsyncMock()
    backend._client = client
    return backend, client


async def _run_polls(backend: DLNABackend, monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    ticks = 0

    async def tick(_delay: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > n:
            backend._is_connected = False

    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.asyncio.sleep", tick)
    await backend._poll_state_loop()


class TestTransportRead:
    async def test_get_state_keeps_last_known_when_soap_fails(self) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(return_value=None)

        assert await backend.get_state() == PlaybackState.PLAYING
        assert await backend._read_transport_state() is None


class TestPollTrackEnd:
    async def test_failed_soap_poll_does_not_end_track(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(return_value=None)
        ended = MagicMock()
        backend.on_track_ended(ended)

        await _run_polls(backend, monkeypatch, 1)

        ended.assert_not_called()
        client.reset_session.assert_awaited_once()

    async def test_one_stopped_poll_does_not_end_track(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(return_value="STOPPED")
        ended = MagicMock()
        backend.on_track_ended(ended)

        await _run_polls(backend, monkeypatch, 1)

        ended.assert_not_called()
        assert PLAYBACK_STOP_CONFIRMATIONS > 1

    async def test_confirmed_stopped_polls_end_track(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(return_value="STOPPED")
        ended = MagicMock()
        backend.on_track_ended(ended)

        await _run_polls(backend, monkeypatch, PLAYBACK_STOP_CONFIRMATIONS)

        ended.assert_called_once()

    async def test_stopped_then_playing_does_not_end_track(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(side_effect=["STOPPED", "PLAYING"])
        ended = MagicMock()
        backend.on_track_ended(ended)

        await _run_polls(backend, monkeypatch, 2)

        ended.assert_not_called()


class TestRendererUnreachable:
    async def test_persistent_unreachable_fires_reconnect_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, client = _playing_backend()
        client.get_transport_info = AsyncMock(return_value=None)
        called = asyncio.Event()

        async def on_unreach() -> None:
            called.set()

        backend.on_renderer_unreachable(on_unreach)
        ended = MagicMock()
        backend.on_track_ended(ended)

        await _run_polls(backend, monkeypatch, 2)
        if backend._unreachable_task:
            await asyncio.wait_for(backend._unreachable_task, timeout=1)

        assert called.is_set()
        ended.assert_not_called()
        assert client.reset_session.await_count >= 1
