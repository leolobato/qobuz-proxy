"""Tests for webui API routes."""

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.webui.routes import register_routes


def make_app(auth_state: dict | None = None) -> web.Application:
    app = web.Application()
    if auth_state is None:
        auth_state = {"authenticated": False, "user_id": "", "email": ""}
    app["auth_state"] = auth_state
    app["get_speakers"] = lambda: []
    app["version"] = "1.3.3"
    app["on_auth_token"] = AsyncMock(return_value=True)
    app["on_logout"] = AsyncMock()
    register_routes(app)
    return app


@pytest.fixture
async def client():
    app = make_app()
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
async def authed_client():
    app = make_app(
        auth_state={"authenticated": True, "user_id": "12345", "email": "user@example.com"}
    )
    app["get_speakers"] = lambda: [
        {
            "id": "living-room",
            "name": "Living Room",
            "backend": "dlna",
            "status": "playing",
            "config": {"dlna_ip": "192.168.1.50", "dlna_port": 1400, "max_quality": 7},
            "now_playing": {
                "title": "Test Track",
                "artist": "Test Artist",
                "album": "Test Album",
                "album_art_url": "",
                "quality": "Hi-Res 96kHz",
                "volume": 50,
            },
        }
    ]
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_status_unauthenticated(client: TestClient) -> None:
    resp = await client.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["auth"]["authenticated"] is False
    assert data["auth"]["user_id"] == ""
    assert data["auth"]["email"] == ""
    assert data["speakers"] == []
    assert data["version"] == "1.3.3"
    assert "uptime" in data


async def test_status_authenticated(authed_client: TestClient) -> None:
    resp = await authed_client.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["auth"]["authenticated"] is True
    assert data["auth"]["user_id"] == "12345"
    assert data["auth"]["email"] == "user@example.com"
    assert len(data["speakers"]) == 1
    speaker = data["speakers"][0]
    assert speaker["id"] == "living-room"
    assert speaker["name"] == "Living Room"
    assert speaker["status"] == "playing"
    assert speaker["now_playing"]["title"] == "Test Track"


async def test_auth_login_redirects_to_qobuz(client: TestClient) -> None:
    resp = await client.get("/auth/login?origin=http://localhost:8689", allow_redirects=False)
    assert resp.status == 302
    location = resp.headers["Location"]
    assert "qobuz.com/signin/oauth" in location
    assert "ext_app_id=304027809" in location
    assert "redirect_url=" in location
    assert "localhost%3A8689" in location or "localhost:8689" in location


async def test_auth_login_missing_origin(client: TestClient) -> None:
    resp = await client.get("/auth/login")
    assert resp.status == 400


async def test_auth_callback_success(client: TestClient) -> None:
    mock_creds = {
        "user_id": "12345",
        "user_auth_token": "secret-token",
        "display_name": "Test User",
        "email": "test@example.com",
        "avatar": "",
    }
    with patch(
        "qobuz_proxy.webui.routes.exchange_code",
        new_callable=AsyncMock,
        return_value=mock_creds,
    ):
        resp = await client.get("/auth/callback?code_autorisation=test-code", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/"
    client.app["on_auth_token"].assert_awaited_once_with(  # type: ignore[union-attr]
        "12345",
        "secret-token",
        {"email": "test@example.com", "name": "Test User", "avatar": ""},
        validated=True,
    )


async def test_auth_callback_missing_code(client: TestClient) -> None:
    resp = await client.get("/auth/callback", allow_redirects=False)
    assert resp.status == 302
    assert "error=missing_code" in resp.headers["Location"]


async def test_auth_callback_exchange_failure(client: TestClient) -> None:
    with patch(
        "qobuz_proxy.webui.routes.exchange_code",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Token exchange failed"),
    ):
        resp = await client.get("/auth/callback?code_autorisation=bad-code", allow_redirects=False)
    assert resp.status == 302
    assert "error=exchange_failed" in resp.headers["Location"]


async def test_auth_callback_auth_failure(client: TestClient) -> None:
    mock_creds = {
        "user_id": "12345",
        "user_auth_token": "bad-token",
        "display_name": "",
        "email": "",
        "avatar": "",
    }
    client.app["on_auth_token"] = AsyncMock(return_value=False)  # type: ignore[union-attr]
    with patch(
        "qobuz_proxy.webui.routes.exchange_code",
        new_callable=AsyncMock,
        return_value=mock_creds,
    ):
        resp = await client.get("/auth/callback?code_autorisation=test-code", allow_redirects=False)
    assert resp.status == 302
    assert "error=auth_failed" in resp.headers["Location"]


async def test_index_uses_root_base_without_ingress(client: TestClient) -> None:
    """Served index.html should carry a root <base href> when not behind ingress."""
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert '<base href="/">' in body
    # Assets are referenced relative to that base.
    assert 'href="static/style.css' in body
    assert 'src="static/app.js' in body


async def test_index_injects_ingress_base(client: TestClient) -> None:
    """The X-Ingress-Path header should become the document <base href>."""
    resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status == 200
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body


async def test_auth_callback_redirects_under_ingress(client: TestClient) -> None:
    """On success the callback should redirect back to the ingress base, not root."""
    mock_creds = {
        "user_id": "12345",
        "user_auth_token": "secret-token",
        "display_name": "Test User",
        "email": "test@example.com",
        "avatar": "",
    }
    with patch(
        "qobuz_proxy.webui.routes.exchange_code",
        new_callable=AsyncMock,
        return_value=mock_creds,
    ):
        resp = await client.get(
            "/auth/callback?code_autorisation=test-code",
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
            allow_redirects=False,
        )
    assert resp.status == 302
    assert resp.headers["Location"] == "/api/hassio_ingress/abc123/"


async def test_auth_callback_error_redirects_under_ingress(client: TestClient) -> None:
    """Error redirects should also stay within the ingress base path."""
    resp = await client.get(
        "/auth/callback",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/api/hassio_ingress/abc123/?error=missing_code"


async def test_auth_logout(client: TestClient) -> None:
    resp = await client.post("/api/auth/logout")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    client.app["on_logout"].assert_awaited_once()  # type: ignore[union-attr]


async def test_status_uptime_format(client: TestClient) -> None:
    """Uptime should be formatted as 'Xh Ym' or 'Xm'."""
    resp = await client.get("/api/status")
    data = await resp.json()
    uptime = data["uptime"]
    # Should match "Xm" or "Xh Ym" pattern
    assert "m" in uptime


def test_format_uptime_breakdown() -> None:
    """Uptime formatting should pick the right unit set for the duration."""
    from qobuz_proxy.webui.routes import _format_uptime

    assert _format_uptime(0) == "0m"
    assert _format_uptime(60 * 5) == "5m"
    assert _format_uptime(60 * 65) == "1h 5m"
    assert _format_uptime(60 * 60 * 25) == "1d 1h 0m"
    assert _format_uptime(60 * 60 * 24 * 3 + 60 * 60 * 4 + 60 * 30) == "3d 4h 30m"
