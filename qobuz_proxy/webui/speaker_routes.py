"""Speaker management API routes: CRUD and discovery."""

import logging
from aiohttp import web
from qobuz_proxy.backends.dlna.discovery import discover_dlna_devices

logger = logging.getLogger(__name__)


async def _handle_discover_dlna(request: web.Request) -> web.Response:
    """Trigger SSDP discovery and return found DLNA devices."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    timeout = float(body.get("timeout", 5))
    devices = await discover_dlna_devices(timeout=timeout)

    result = {
        "devices": [
            {
                "friendly_name": d.friendly_name,
                "ip": d.ip,
                "port": d.port,
                "model_name": d.model_name,
                "manufacturer": d.manufacturer,
                "udn": d.udn,
                "location": d.location,
            }
            for d in devices
        ],
        "count": len(devices),
    }
    return web.json_response(result)


async def _handle_discover_audio_devices(request: web.Request) -> web.Response:
    """List local audio output devices. Returns 404 if disabled."""
    if not request.app.get("local_audio_enabled", False):
        return web.json_response({"error": "local_audio_ui_disabled"}, status=404)

    try:
        from qobuz_proxy.backends.local.device import list_audio_devices

        devices = list_audio_devices()
        result = {
            "devices": [
                {
                    "name": d.name,
                    "index": d.index,
                    "channels": d.channels,
                    "sample_rate": int(d.default_samplerate),
                    "is_default": d.is_default,
                }
                for d in devices
            ],
        }
        return web.json_response(result)
    except ImportError:
        return web.json_response({"error": "local_audio_dependencies_missing"}, status=404)


async def _handle_get_speakers(request: web.Request) -> web.Response:
    """Return all speakers with config and live status."""
    speakers = request.app["get_speakers"]()
    return web.json_response(speakers)


async def _handle_add_speaker(request: web.Request) -> web.Response:
    """Add a new speaker."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = body.get("name", "").strip()
    backend = body.get("backend", "dlna")

    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    if backend == "dlna" and not body.get("dlna_ip"):
        return web.json_response({"error": "dlna_ip is required for DLNA backend"}, status=400)

    callback = request.app["on_add_speaker"]
    try:
        result = await callback(body)
        return web.json_response(result, status=201)
    except (ValueError, Exception) as e:
        return web.json_response({"error": str(e)}, status=400)


async def _handle_edit_speaker(request: web.Request) -> web.Response:
    """Edit an existing speaker."""
    speaker_id = request.match_info["speaker_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    callback = request.app["on_edit_speaker"]
    try:
        result = await callback(speaker_id, body)
        return web.json_response(result)
    except KeyError:
        return web.json_response({"error": "speaker not found"}, status=404)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


async def _handle_speaker_control(request: web.Request) -> web.Response:
    """Play, pause, skip, or set volume on a running speaker."""
    speaker_id = request.match_info["speaker_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(body, dict) or not body.get("action"):
        return web.json_response({"error": "action is required"}, status=400)

    callback = request.app["on_speaker_control"]
    try:
        result = await callback(speaker_id, body)
        return web.json_response(result)
    except KeyError:
        return web.json_response({"error": "speaker not found"}, status=404)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except RuntimeError as e:
        return web.json_response({"error": str(e)}, status=409)


async def _handle_remove_speaker(request: web.Request) -> web.Response:
    """Remove a speaker."""
    speaker_id = request.match_info["speaker_id"]

    callback = request.app["on_remove_speaker"]
    try:
        await callback(speaker_id)
        return web.Response(status=204)
    except KeyError:
        return web.json_response({"error": "speaker not found"}, status=404)


def register_speaker_routes(app: web.Application) -> None:
    """Register speaker management routes."""
    app.router.add_post("/api/discover/dlna", _handle_discover_dlna)
    app.router.add_get("/api/discover/audio-devices", _handle_discover_audio_devices)
    app.router.add_get("/api/speakers", _handle_get_speakers)
    app.router.add_post("/api/speakers", _handle_add_speaker)
    app.router.add_put("/api/speakers/{speaker_id}", _handle_edit_speaker)
    app.router.add_post("/api/speakers/{speaker_id}/control", _handle_speaker_control)
    app.router.add_delete("/api/speakers/{speaker_id}", _handle_remove_speaker)
