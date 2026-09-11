"""
mDNS registration and HTTP discovery endpoints.

Handles device discovery via mDNS and connection handshake via HTTP.
"""

import asyncio
import json
import logging
import socket
from typing import Any, Callable, Optional

from aiohttp import web
from zeroconf import IPVersion, ServiceInfo, Zeroconf

from qobuz_proxy.config import Config

from .types import ConnectTokens, JWTApiToken, JWTConnectToken

logger = logging.getLogger(__name__)

# mDNS constants
MDNS_SERVICE_TYPE = "_qobuz-connect._tcp.local."
SDK_VERSION = "py-1.0.0"

# Quality ID to HTTP display string mapping
QUALITY_TO_HTTP = {
    5: "MP3",
    6: "LOSSLESS",
    7: "HIRES_L1",
    27: "HIRES_L3",
}

# Quality getter callback type
QualityGetter = Callable[[], int]


def _sanitize_service_name(name: str) -> str:
    """
    Sanitize device name for use in mDNS service name.

    Replaces spaces and other problematic characters with hyphens
    to ensure compatibility with mDNS/DNS-SD discovery.

    Args:
        name: Device display name (may contain spaces)

    Returns:
        Sanitized name safe for mDNS service registration
    """
    # Replace spaces with hyphens
    sanitized = name.replace(" ", "-")
    # Remove any other potentially problematic characters
    # Keep only alphanumeric, hyphens, and underscores
    sanitized = "".join(c if c.isalnum() or c in "-_" else "-" for c in sanitized)
    # Collapse multiple hyphens
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    # Strip leading/trailing hyphens
    return sanitized.strip("-")


class DiscoveryService:
    """
    Manages mDNS registration and HTTP discovery endpoints.

    Exposes QobuzProxy to the Qobuz app and handles the connection
    handshake where the app provides JWT tokens.
    """

    def __init__(
        self,
        config: Config,
        app_id: str,
        on_connect: Optional[Callable[[ConnectTokens], None]] = None,
        quality_getter: Optional[QualityGetter] = None,
        web_app: Optional[web.Application] = None,
    ):
        """
        Initialize discovery service.

        Args:
            config: Application configuration
            app_id: Qobuz app ID (from credential scraper)
            on_connect: Callback when app connects with tokens
            quality_getter: Callback to get current max quality setting
            web_app: Optional shared aiohttp Application. When provided,
                routes are registered on it but server lifecycle is not managed.
        """
        self.config = config
        self.app_id = app_id
        self.on_connect = on_connect
        self._quality_getter = quality_getter

        # HTTP server components
        if web_app is not None:
            self._app: Optional[web.Application] = web_app
            self._owns_server = False
        else:
            self._app = None
            self._owns_server = True
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # mDNS components
        self._zeroconf: Optional[Zeroconf] = None
        self._service_info: Optional[ServiceInfo] = None

        # State
        self._current_session_id: str = ""
        self._received_tokens: Optional[ConnectTokens] = None

    async def start(self) -> None:
        """Start HTTP server and register mDNS service."""
        await self._start_http_server()
        await self._register_mdns()
        logger.info(f"Discovery service started on port {self.config.server.http_port}")

    async def stop(self) -> None:
        """Stop HTTP server and unregister mDNS service."""
        await self._unregister_mdns()
        await self._stop_http_server()
        logger.info("Discovery service stopped")

    def get_received_tokens(self) -> Optional[ConnectTokens]:
        """Get tokens received from last connection request."""
        return self._received_tokens

    # -------------------------------------------------------------------------
    # HTTP Server
    # -------------------------------------------------------------------------

    async def _start_http_server(self) -> None:
        """Start aiohttp server with discovery endpoints."""
        if self._owns_server:
            self._app = web.Application()
            self._app.router.add_get("/", self._handle_root)

        assert self._app is not None
        self._app.router.add_get("/streamcore/get-display-info", self._handle_display_info)
        self._app.router.add_get("/streamcore/get-connect-info", self._handle_connect_info)
        self._app.router.add_post("/streamcore/connect-to-qconnect", self._handle_connect)

        if self._owns_server:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()

            self._site = web.TCPSite(
                self._runner,
                self.config.server.bind_address,
                self.config.server.http_port,
            )
            await self._site.start()

    async def _stop_http_server(self) -> None:
        """Stop aiohttp server (only if we own it)."""
        if self._owns_server:
            if self._site:
                await self._site.stop()
            if self._runner:
                await self._runner.cleanup()

    async def _handle_root(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.Response(
            text=f"qobuz-proxy - {self.config.device.name}",
            content_type="text/plain",
        )

    async def _handle_display_info(self, request: web.Request) -> web.Response:
        """
        GET /streamcore/get-display-info

        Returns device metadata for display in the Qobuz app.
        """
        # Get current quality from getter callback, default to Hi-Res 192k
        quality_id = 27
        if self._quality_getter:
            quality_id = self._quality_getter()
        quality_str = QUALITY_TO_HTTP.get(quality_id, "HIRES_L3")

        response = {
            "type": "SPEAKER",
            "friendly_name": self.config.device.name,
            "model_display_name": "qobuz-proxy",
            "brand_display_name": "qobuz-proxy",
            "serial_number": self.config.device.uuid,
            "max_audio_quality": quality_str,
        }
        return web.json_response(response)

    async def _handle_connect_info(self, request: web.Request) -> web.Response:
        """
        GET /streamcore/get-connect-info

        Returns app ID and session information.
        """
        response = {
            "current_session_id": self._current_session_id,
            "app_id": self.app_id,
        }
        return web.json_response(response)

    async def _handle_connect(self, request: web.Request) -> web.Response:
        """
        POST /streamcore/connect-to-qconnect

        Receives JWT tokens from the Qobuz app.
        """
        try:
            data = await request.json()
            logger.debug(f"Received connect request: {list(data.keys())}")

            # Parse tokens
            tokens = self._parse_connect_request(data)

            if not tokens.is_valid():
                logger.error("Invalid tokens in connect request")
                return web.json_response({"error": "Invalid tokens"}, status=400)

            # Store tokens
            self._received_tokens = tokens
            self._current_session_id = tokens.session_id

            logger.info(f"Received connection from app (session: {tokens.session_id[:8]}...)")

            # Notify callback
            if self.on_connect:
                self.on_connect(tokens)

            return web.json_response({})

        except json.JSONDecodeError:
            logger.error("Invalid JSON in connect request")
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.exception(f"Error handling connect request: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def _parse_connect_request(self, data: dict[str, Any]) -> ConnectTokens:
        """Parse connect request JSON into ConnectTokens."""
        tokens = ConnectTokens()
        tokens.session_id = data.get("session_id", "")

        jwt_qconnect = data.get("jwt_qconnect", {})
        if jwt_qconnect:
            tokens.ws_token = JWTConnectToken(
                jwt=jwt_qconnect.get("jwt", ""),
                exp=jwt_qconnect.get("exp", 0),
                endpoint=jwt_qconnect.get("endpoint", ""),
            )

        jwt_api = data.get("jwt_api", {})
        if jwt_api:
            tokens.api_token = JWTApiToken(
                jwt=jwt_api.get("jwt", ""),
                exp=jwt_api.get("exp", 0),
            )

        return tokens

    # -------------------------------------------------------------------------
    # mDNS Registration
    # -------------------------------------------------------------------------

    async def _register_mdns(self) -> None:
        """Register mDNS service."""
        local_ip = self._get_local_ip()
        if not local_ip:
            logger.warning("Could not determine local IP, mDNS may not work")
            return

        # Sanitize device name for mDNS service name (no spaces allowed)
        # The display name in properties["Name"] keeps the original formatting
        sanitized_name = _sanitize_service_name(self.config.device.name)
        service_name = f"{sanitized_name}.{MDNS_SERVICE_TYPE}"

        properties = {
            "path": "/streamcore",
            "type": "SPEAKER",
            "sdk_version": SDK_VERSION,
            "Name": self.config.device.name,  # Original name for display
            "device_uuid": self.config.device.uuid,
        }

        self._service_info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            service_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.config.server.http_port,
            properties=properties,
        )

        # Advertise only on the LAN address the phone can reach. Zeroconf()
        # otherwise joins every interface (VPN utun, AWDL, …) and the Qobuz
        # app on Wi-Fi never sees the speaker even though the Web UI at this IP
        # loads fine.
        try:
            self._zeroconf = Zeroconf(interfaces=[local_ip], ip_version=IPVersion.V4Only)
        except Exception:
            logger.warning(
                "mDNS could not bind to %s; advertising on all interfaces", local_ip
            )
            self._zeroconf = Zeroconf()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._zeroconf.register_service, self._service_info)
        except Exception:
            # Name may conflict with a stale entry from a previous run — re-register as owner
            await loop.run_in_executor(
                None,
                lambda: self._zeroconf.register_service(
                    self._service_info, cooperating_responders=True
                ),
            )

        logger.info(
            f"Registered mDNS service: {self.config.device.name} "
            f"(as {sanitized_name}) at {local_ip}:{self.config.server.http_port}"
        )

    async def _unregister_mdns(self) -> None:
        """Unregister mDNS service."""
        if self._zeroconf and self._service_info:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._zeroconf.unregister_service, self._service_info)
            await loop.run_in_executor(None, self._zeroconf.close)
            logger.debug("Unregistered mDNS service")

    def _get_local_ip(self) -> Optional[str]:
        """IPv4 the Qobuz app on Wi-Fi can reach.

        Prefer a real LAN NIC over the default-route address: a VPN makes
        the 8.8.8.8 trick return a utun IP the phone cannot use, while the
        Web UI is still opened at the Wi-Fi address.
        """
        lan = _lan_ipv4()
        if lan:
            return lan
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
            return str(ip)
        except Exception as e:
            logger.error(f"Failed to determine local IP: {e}")
            return None


_SKIP_IFACE_PREFIXES = ("lo", "utun", "awdl", "llw", "bridge", "anpi", "ap", "gif", "stf", "vmnet")


def _lan_ipv4() -> Optional[str]:
    """First non-loopback IPv4 on a broadcast NIC (en0/eth0), not VPN."""
    try:
        import ifaddr
    except ImportError:
        return None
    for adapter in ifaddr.get_adapters():
        name = (adapter.nice_name or adapter.name or "").lower()
        if name.startswith(_SKIP_IFACE_PREFIXES):
            continue
        for ip in adapter.ips:
            addr = ip.ip if isinstance(ip.ip, str) else None
            if not addr or "." not in addr:
                continue
            if addr.startswith("127.") or addr.startswith("169.254."):
                continue
            return addr
    return None
