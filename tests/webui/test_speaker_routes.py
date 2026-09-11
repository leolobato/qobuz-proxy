"""Tests for speaker management API routes."""

import pytest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from qobuz_proxy.webui.routes import register_routes


def make_app() -> web.Application:
    app = web.Application()
    app["auth_state"] = {"authenticated": True, "user_id": "12345", "email": "user@example.com"}
    app["get_speakers"] = lambda: []
    app["version"] = "1.3.3"
    app["on_auth_token"] = AsyncMock(return_value=True)
    app["on_logout"] = AsyncMock()
    app["on_add_speaker"] = AsyncMock(return_value={"id": "test", "name": "Test"})
    app["on_edit_speaker"] = AsyncMock(return_value={"id": "test", "name": "Test"})
    app["on_remove_speaker"] = AsyncMock(return_value=True)
    app["on_speaker_control"] = AsyncMock(return_value={"ok": True, "id": "living-room"})
    app["local_audio_enabled"] = False
    register_routes(app)
    return app


@pytest.fixture
async def client():
    async with TestClient(TestServer(make_app())) as c:
        yield c


class TestDiscoverDLNA:
    async def test_discover_returns_devices(self, client: TestClient) -> None:
        mock_device = AsyncMock()
        mock_device.friendly_name = "Sonos One"
        mock_device.ip = "192.168.1.50"
        mock_device.port = 1400
        mock_device.model_name = "Sonos One"
        mock_device.manufacturer = "Sonos, Inc."
        mock_device.udn = "uuid:123"
        mock_device.location = "http://192.168.1.50:1400/xml/desc.xml"

        with patch(
            "qobuz_proxy.webui.speaker_routes.discover_dlna_devices",
            new_callable=AsyncMock,
            return_value=[mock_device],
        ):
            resp = await client.post("/api/discover/dlna", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["count"] == 1
            assert data["devices"][0]["friendly_name"] == "Sonos One"
            assert data["devices"][0]["ip"] == "192.168.1.50"
            assert data["devices"][0]["location"] == "http://192.168.1.50:1400/xml/desc.xml"

    async def test_discover_with_timeout(self, client: TestClient) -> None:
        with patch(
            "qobuz_proxy.webui.speaker_routes.discover_dlna_devices",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_discover:
            resp = await client.post("/api/discover/dlna", json={"timeout": 10})
            assert resp.status == 200
            mock_discover.assert_awaited_once_with(timeout=10.0)

    async def test_discover_empty(self, client: TestClient) -> None:
        with patch(
            "qobuz_proxy.webui.speaker_routes.discover_dlna_devices",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = await client.post("/api/discover/dlna", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["count"] == 0
            assert data["devices"] == []


class TestDiscoverAudioDevices:
    async def test_returns_404_when_disabled(self, client: TestClient) -> None:
        resp = await client.get("/api/discover/audio-devices")
        assert resp.status == 404

    async def test_returns_devices_when_enabled(self) -> None:
        app = make_app()
        app["local_audio_enabled"] = True
        async with TestClient(TestServer(app)) as c:
            mock_device = AsyncMock()
            mock_device.index = 0
            mock_device.name = "Built-in Output"
            mock_device.channels = 2
            mock_device.default_samplerate = 44100.0
            mock_device.is_default = True

            with patch(
                "qobuz_proxy.backends.local.device.list_audio_devices",
                return_value=[mock_device],
            ):
                resp = await c.get("/api/discover/audio-devices")
                assert resp.status == 200
                data = await resp.json()
                assert len(data["devices"]) == 1
                assert data["devices"][0]["name"] == "Built-in Output"
                assert data["devices"][0]["is_default"] is True


class TestSpeakerCRUD:
    async def test_get_speakers(self, client: TestClient) -> None:
        resp = await client.get("/api/speakers")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    async def test_add_dlna_speaker(self, client: TestClient) -> None:
        client.app["on_add_speaker"] = AsyncMock(
            return_value={
                "id": "living-room",
                "name": "Living Room",
                "backend": "dlna",
                "status": "idle",
                "config": {"dlna_ip": "192.168.1.50", "dlna_port": 1400, "max_quality": 7},
                "now_playing": None,
            }
        )
        resp = await client.post(
            "/api/speakers",
            json={
                "name": "Living Room",
                "backend": "dlna",
                "dlna_ip": "192.168.1.50",
                "dlna_port": 1400,
                "max_quality": 7,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["id"] == "living-room"

    async def test_add_speaker_missing_name(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/speakers", json={"backend": "dlna", "dlna_ip": "192.168.1.50"}
        )
        assert resp.status == 400

    async def test_add_speaker_missing_dlna_ip(self, client: TestClient) -> None:
        resp = await client.post("/api/speakers", json={"name": "Test", "backend": "dlna"})
        assert resp.status == 400

    async def test_add_speaker_callback_error(self, client: TestClient) -> None:
        client.app["on_add_speaker"] = AsyncMock(side_effect=ValueError("Duplicate speaker name"))
        resp = await client.post(
            "/api/speakers", json={"name": "Dup", "backend": "dlna", "dlna_ip": "10.0.0.1"}
        )
        assert resp.status == 400
        data = await resp.json()
        assert "Duplicate" in data["error"]

    async def test_edit_speaker(self, client: TestClient) -> None:
        client.app["on_edit_speaker"] = AsyncMock(
            return_value={
                "id": "living-room",
                "name": "Living Room",
                "backend": "dlna",
                "status": "idle",
                "config": {},
                "now_playing": None,
            }
        )
        resp = await client.put(
            "/api/speakers/living-room", json={"name": "Living Room", "dlna_ip": "192.168.1.51"}
        )
        assert resp.status == 200

    async def test_edit_speaker_not_found(self, client: TestClient) -> None:
        client.app["on_edit_speaker"] = AsyncMock(side_effect=KeyError("not-found"))
        resp = await client.put("/api/speakers/not-found", json={"name": "X"})
        assert resp.status == 404

    async def test_remove_speaker(self, client: TestClient) -> None:
        resp = await client.delete("/api/speakers/living-room")
        assert resp.status == 204

    async def test_remove_speaker_not_found(self, client: TestClient) -> None:
        client.app["on_remove_speaker"] = AsyncMock(side_effect=KeyError("not-found"))
        resp = await client.delete("/api/speakers/not-found")
        assert resp.status == 404


class TestSpeakerControlRoute:
    async def test_pause(self, client: TestClient) -> None:
        resp = await client.post("/api/speakers/living-room/control", json={"action": "pause"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        client.app["on_speaker_control"].assert_awaited_once_with(
            "living-room", {"action": "pause"}
        )

    async def test_volume(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/speakers/living-room/control", json={"action": "volume", "volume": 30}
        )
        assert resp.status == 200
        client.app["on_speaker_control"].assert_awaited_once()

    async def test_seek(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/speakers/living-room/control",
            json={"action": "seek", "position_ms": 15000},
        )
        assert resp.status == 200
        client.app["on_speaker_control"].assert_awaited_once_with(
            "living-room", {"action": "seek", "position_ms": 15000}
        )

    async def test_missing_action(self, client: TestClient) -> None:
        resp = await client.post("/api/speakers/living-room/control", json={})
        assert resp.status == 400

    async def test_not_found(self, client: TestClient) -> None:
        client.app["on_speaker_control"] = AsyncMock(side_effect=KeyError("missing"))
        resp = await client.post("/api/speakers/missing/control", json={"action": "pause"})
        assert resp.status == 404

    async def test_not_running(self, client: TestClient) -> None:
        client.app["on_speaker_control"] = AsyncMock(side_effect=RuntimeError("speaker is not running"))
        resp = await client.post("/api/speakers/living-room/control", json={"action": "pause"})
        assert resp.status == 409
