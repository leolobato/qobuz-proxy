"""Web UI API routes for status and authentication."""

import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from qobuz_proxy.auth.oauth import build_oauth_url, exchange_code, extract_code
from qobuz_proxy.webui.speaker_routes import register_speaker_routes

logger = logging.getLogger(__name__)

_start_time = time.monotonic()

_STATIC_DIR = Path(__file__).parent / "static"


def _format_uptime(seconds: float) -> str:
    """Format uptime as 'Xd Yh Zm', 'Xh Ym', or 'Xm'."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _ingress_base(request: web.Request) -> str:
    """Return the base path the UI is served under, with a trailing slash.

    Home Assistant ingress forwards requests with an ``X-Ingress-Path`` header
    holding the path prefix (e.g. ``/api/hassio_ingress/<token>``). Using it as
    the document ``<base href>`` lets all relative links/fetches resolve both
    behind ingress and on direct ``http://host:8689/`` access.
    """
    ingress_path = request.headers.get("X-Ingress-Path", "")
    return f"{ingress_path}/" if ingress_path else "/"


async def _handle_index(request: web.Request) -> web.Response:
    """Serve index.html with version-based cache busting."""
    html = (_STATIC_DIR / "index.html").read_text()
    html = html.replace("{{version}}", request.app.get("version", "0"))
    html = html.replace("{{base}}", _ingress_base(request))
    return web.Response(text=html, content_type="text/html")


async def _handle_status(request: web.Request) -> web.Response:
    """Return JSON with auth state, speakers, and system info."""
    app = request.app
    uptime_seconds = time.monotonic() - _start_time
    data: dict[str, Any] = {
        "auth": app["auth_state"],
        "speakers": app["get_speakers"](),
        "version": app["version"],
        "commit": app.get("commit", ""),
        "uptime": _format_uptime(uptime_seconds),
    }
    return web.json_response(data)


async def _handle_auth_login(request: web.Request) -> web.Response:
    """Redirect the user to the Qobuz OAuth sign-in page."""
    origin = request.query.get("origin", "")
    if not origin:
        return web.json_response({"error": "missing origin parameter"}, status=400)
    redirect_url = f"{origin}/auth/callback"
    oauth_url = build_oauth_url(redirect_url)
    raise web.HTTPFound(oauth_url)


async def _handle_auth_callback(request: web.Request) -> web.Response:
    """Handle the OAuth redirect from Qobuz, exchange code, and authenticate."""
    base = _ingress_base(request)

    try:
        code = extract_code(str(request.url))
    except ValueError:
        raise web.HTTPFound(f"{base}?error=missing_code")

    try:
        creds = await exchange_code(code)
    except Exception:
        logger.exception("OAuth code exchange failed")
        raise web.HTTPFound(f"{base}?error=exchange_failed")

    profile = {
        "email": creds.get("email", ""),
        "name": creds.get("display_name", ""),
        "avatar": creds.get("avatar", ""),
    }

    callback = request.app["on_auth_token"]
    success: bool = await callback(
        creds["user_id"], creds["user_auth_token"], profile, validated=True
    )

    if not success:
        raise web.HTTPFound(f"{base}?error=auth_failed")

    raise web.HTTPFound(base)


async def _handle_logout(request: web.Request) -> web.Response:
    """Clear auth token via callback."""
    callback = request.app["on_logout"]
    await callback()
    return web.json_response({"status": "ok"})


def register_routes(app: web.Application) -> None:
    """Register all web UI routes on the given application."""
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/status", _handle_status)
    app.router.add_get("/auth/login", _handle_auth_login)
    app.router.add_get("/auth/callback", _handle_auth_callback)
    app.router.add_post("/api/auth/logout", _handle_logout)
    register_speaker_routes(app)
    app.router.add_static("/static", _STATIC_DIR)
