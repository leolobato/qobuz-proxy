"""Tests for DLNA SOAP error reporting and connection/transport recovery."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.backends.dlna import client as client_module
from qobuz_proxy.backends.dlna.client import DLNAClient


class _RaisingPost:
    """Async context manager whose entry raises the given exception."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        raise self._exc

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession that always fails post()."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _RaisingPost(self._exc)


class TestSoapErrorReporting:
    async def test_timeout_logs_exception_type_and_resets_session(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A connection/timeout failure must log a non-empty detail and reset the session."""
        # Avoid real retry sleeps.
        monkeypatch.setattr(client_module, "RETRY_DELAY_SECONDS", 0)

        client = DLNAClient(ip="1.2.3.4")
        # TimeoutError stringifies to an empty string — the old code logged nothing useful.
        client._session = _FakeSession(asyncio.TimeoutError())  # type: ignore[assignment]
        reset_mock = AsyncMock()
        monkeypatch.setattr(client, "reset_session", reset_mock)

        with caplog.at_level("WARNING"):
            result = await client._soap_action_detailed(
                "http://1.2.3.4/ctrl",
                client_module.UPNP_AV_TRANSPORT,
                "Play",
                {"InstanceID": "0"},
            )

        assert result.success is False
        # The log must identify the exception type even though str(e) is empty.
        assert "TimeoutError" in caplog.text
        # Session is recreated once per failed attempt so retries get a fresh socket.
        assert reset_mock.await_count == client_module.MAX_RETRIES


class TestTransportRecovery:
    async def _make_backend(self):  # type: ignore[no-untyped-def]
        """Build a DLNABackend with a mocked client, bypassing real connection."""
        from qobuz_proxy.backends.dlna.backend import DLNABackend

        backend = DLNABackend.__new__(DLNABackend)
        # Minimal attributes used by _play_via_transport / _try_transport_sequence.
        backend._client = AsyncMock()
        backend._notify_playback_error = lambda msg: None  # type: ignore[assignment]
        return backend

    async def test_recovers_after_first_seturi_failure(self) -> None:
        """First SetAVTransportURI fails; after reset+stop the retry succeeds."""
        backend = await self._make_backend()
        client = backend._client
        # set_av_transport_uri: fail first, succeed on retry. play: succeed.
        client.set_av_transport_uri = AsyncMock(side_effect=[False, True])
        client.play = AsyncMock(return_value=True)

        ok = await backend._play_via_transport("http://proxy/1.flac", "<didl/>")

        assert ok is True
        # Recovery path ran: session reset + transport stop before the retry.
        client.reset_session.assert_awaited_once()
        client.stop.assert_awaited_once()
        assert client.set_av_transport_uri.await_count == 2

    async def test_reports_error_when_recovery_also_fails(self) -> None:
        """If the retry also fails, a playback error is reported (no silent hang)."""
        backend = await self._make_backend()
        client = backend._client
        client.set_av_transport_uri = AsyncMock(return_value=False)
        client.play = AsyncMock(return_value=True)

        errors = []
        backend._notify_playback_error = lambda msg: errors.append(msg)  # type: ignore[assignment]

        ok = await backend._play_via_transport("http://proxy/1.flac", "<didl/>")

        assert ok is False
        assert errors == ["Failed to set transport URI"]
        client.reset_session.assert_awaited_once()
