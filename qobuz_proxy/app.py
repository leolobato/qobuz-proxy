"""
QobuzProxy Application.

Orchestrates authentication, the web UI, and per-speaker lifecycle.
The HTTP server starts first so users can submit credentials through the
web UI even before a valid Qobuz token is available.
"""

import asyncio
import logging
import os
import signal
from typing import Optional

from aiohttp import web

from qobuz_proxy import __commit__, __version__
from qobuz_proxy.auth import (
    QobuzAPIClient,
    clear_user_token,
    load_user_token,
    save_user_token,
)
from qobuz_proxy.auth.oauth import OAUTH_APP_ID, OAUTH_APP_SECRET
from qobuz_proxy.config import (
    AUTO_QUALITY,
    Config,
    SpeakerConfig,
    _assign_ports,
    _generate_uuids,
    slugify_name,
)
from qobuz_proxy.speaker import Speaker
from qobuz_proxy.webui.config_writer import save_config
from qobuz_proxy.webui.routes import register_routes

logger = logging.getLogger(__name__)


class QobuzProxy:
    """
    Main QobuzProxy application.

    Starts the shared HTTP server (web UI + discovery routes) first, then
    attempts automatic authentication from config or cached tokens. If no
    valid credentials are available the app stays running in a
    "waiting-for-auth" state so the user can provide a token through the
    web UI.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._is_running = False
        self._shutdown_event = asyncio.Event()

        # Auth / API
        self._api_client: Optional[QobuzAPIClient] = None
        self._app_id: str = ""
        self._app_secret: str = ""

        # Auth state — shared with the web UI status endpoint via _web_app["auth_state"].
        # Always the *same* dict object so route handlers see live updates.
        self._auth_state: dict[str, object] = {
            "authenticated": False,
            "user_id": "",
            "email": "",
            "name": "",
            "avatar": "",
        }

        # Shared aiohttp application (web UI + per-speaker discovery routes)
        self._web_app: Optional[web.Application] = None
        self._web_runner: Optional[web.AppRunner] = None
        self._web_site: Optional[web.TCPSite] = None

        # Speakers
        self._speakers: list[Speaker] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start web server, fetch app credentials, attempt auto-auth."""
        logger.info("Starting qobuz-proxy...")

        # 1. Start the HTTP server so the web UI is reachable immediately
        await self._start_web_server()

        # 2. Set Qobuz app credentials (desktop app OAuth + signing secret)
        self._app_id = OAUTH_APP_ID
        self._app_secret = OAUTH_APP_SECRET

        # 3. Attempt auto-auth from config or cache
        token_info = self._get_token_from_config_or_cache()
        if token_info:
            user_id = token_info["user_id"]
            auth_token = token_info["user_auth_token"]
            email = token_info.get("email", "")

            if await self._authenticate(user_id, auth_token):
                self._auth_state["user_id"] = user_id
                self._auth_state["email"] = email
                self._auth_state["authenticated"] = True
                await self._start_speakers()
            else:
                logger.warning("Cached/config token is invalid — waiting for auth via web UI")

        if not self._auth_state["authenticated"]:
            port = self._config.server.http_port
            logger.info(f"No valid credentials — visit http://localhost:{port} to authenticate")

        self._is_running = True

    async def stop(self) -> None:
        """Stop speakers, then the web server."""
        if not self._is_running:
            return

        self._is_running = False
        await self._stop_speakers()
        await self._stop_web_server()
        logger.info("qobuz-proxy stopped")

    async def run(self) -> None:
        """Run until SIGINT / SIGTERM."""
        loop = asyncio.get_running_loop()

        def handle_signal() -> None:
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

        try:
            await self.start()
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    @property
    def is_running(self) -> bool:
        """Return True if the application event loop is active."""
        return self._is_running

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def _get_token_from_config_or_cache(self) -> Optional[dict[str, str]]:
        """Return user credentials from config (highest priority) or cache."""
        # Config values take precedence
        if self._config.qobuz.auth_token and self._config.qobuz.user_id:
            return {
                "user_id": self._config.qobuz.user_id,
                "user_auth_token": self._config.qobuz.auth_token,
                "email": self._config.qobuz.email,
            }

        # Fall back to cached token
        cached = load_user_token()
        if cached:
            logger.info("Found cached user token")
            return cached

        return None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, user_id: str, auth_token: str) -> bool:
        """Validate credentials against the Qobuz API. Returns True on success."""
        if not self._app_id:
            logger.error("Cannot authenticate — app credentials not available")
            return False

        self._api_client = QobuzAPIClient(self._app_id, self._app_secret)
        logger.info(f"Authenticating user {user_id}...")
        if await self._api_client.login_with_token(user_id=user_id, auth_token=auth_token):
            logger.info("Authentication successful")
            return True

        logger.warning("Authentication failed — invalid credentials")
        self._api_client = None
        return False

    # ------------------------------------------------------------------
    # Web UI callbacks
    # ------------------------------------------------------------------

    async def _on_auth_token(
        self,
        user_id: str,
        auth_token: str,
        profile: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> bool:
        """Called by the web UI when the user submits a token.

        Validates credentials, persists them to cache, and starts speakers
        if they are not already running.
        """
        if profile is None:
            profile = {}

        # Set up API client directly — token is pre-validated by OAuth
        # and scoped to OAUTH_APP_ID which we use for all requests.
        self._api_client = QobuzAPIClient(self._app_id, self._app_secret)
        self._api_client.user_auth_token = auth_token
        self._api_client.user_id = user_id

        email = profile.get("email", "")
        name = profile.get("name", "")
        avatar = profile.get("avatar", "")

        # Persist to cache
        save_user_token(user_id=user_id, auth_token=auth_token, email=email)

        # Update shared auth state
        self._auth_state["authenticated"] = True
        self._auth_state["user_id"] = user_id
        self._auth_state["email"] = email
        self._auth_state["name"] = name
        self._auth_state["avatar"] = avatar

        # Start speakers if not already running
        if not self._speakers:
            await self._start_speakers()

        return True

    async def _on_logout(self) -> None:
        """Called by the web UI when the user requests logout."""
        logger.info("Logout requested — stopping speakers and clearing token")
        await self._stop_speakers()

        self._auth_state["authenticated"] = False
        self._auth_state["user_id"] = ""
        self._auth_state["email"] = ""
        self._api_client = None

        clear_user_token()

    # ------------------------------------------------------------------
    # Speaker management (hot add / edit / remove)
    # ------------------------------------------------------------------

    async def _on_add_speaker(self, body: dict) -> dict:
        """Add a new speaker at runtime."""
        name = body["name"].strip()
        backend_type = body.get("backend", "dlna")

        # Check for duplicate names
        for s in self._speakers:
            if slugify_name(s.name) == slugify_name(name):
                raise ValueError(f"Speaker '{name}' already exists")

        # Build SpeakerConfig
        quality_raw = body.get("max_quality", "auto")
        if isinstance(quality_raw, str) and quality_raw.lower() == "auto":
            max_quality = AUTO_QUALITY
        else:
            max_quality = int(quality_raw)

        sc = SpeakerConfig(
            name=name,
            backend_type=backend_type,
            max_quality=max_quality,
            dlna_ip=body.get("dlna_ip", ""),
            dlna_port=int(body.get("dlna_port", 1400)),
            dlna_fixed_volume=bool(body.get("fixed_volume", False)),
            dlna_description_url=body.get("description_url", ""),
            audio_device=body.get("audio_device", "default"),
            audio_buffer_size=int(body.get("buffer_size", 2048)),
        )

        # Assign ports and UUID
        all_configs = [s._config for s in self._speakers] + [sc]
        _assign_ports(all_configs, webui_port=self._config.server.http_port)
        _generate_uuids([sc])

        # Create and start speaker
        assert self._api_client is not None
        speaker = Speaker(config=sc, api_client=self._api_client, app_id=self._app_id)
        started = await speaker.start()
        if not started:
            raise ValueError(f"Speaker '{name}' failed to start")

        self._speakers.append(speaker)

        # Update config and persist
        self._config.speakers.append(sc)
        self._save_config()

        return speaker.get_status()

    async def _on_edit_speaker(self, speaker_id: str, body: dict) -> dict:
        """Edit a speaker at runtime (stop, reconfigure, restart)."""
        idx = None
        for i, s in enumerate(self._speakers):
            if slugify_name(s.name) == speaker_id:
                idx = i
                break
        if idx is None:
            raise KeyError(speaker_id)

        old_speaker = self._speakers[idx]
        old_config = self._config.speakers[idx]

        quality_raw = body.get("max_quality", old_config.max_quality)
        if isinstance(quality_raw, str) and quality_raw.lower() == "auto":
            max_quality = AUTO_QUALITY
        else:
            max_quality = int(quality_raw)

        new_config = SpeakerConfig(
            name=body.get("name", old_config.name).strip(),
            uuid=old_config.uuid,
            backend_type=old_config.backend_type,  # Immutable
            max_quality=max_quality,
            http_port=old_config.http_port,
            bind_address=old_config.bind_address,
            dlna_ip=body.get("dlna_ip", old_config.dlna_ip),
            dlna_port=int(body.get("dlna_port", old_config.dlna_port)),
            dlna_fixed_volume=bool(body.get("fixed_volume", old_config.dlna_fixed_volume)),
            dlna_description_url=body.get("description_url", old_config.dlna_description_url),
            proxy_port=old_config.proxy_port,
            audio_device=body.get("audio_device", old_config.audio_device),
            audio_buffer_size=int(body.get("buffer_size", old_config.audio_buffer_size)),
        )

        await old_speaker.stop()

        assert self._api_client is not None
        new_speaker = Speaker(config=new_config, api_client=self._api_client, app_id=self._app_id)
        started = await new_speaker.start()
        if not started:
            await old_speaker.start()
            raise ValueError(f"Speaker '{new_config.name}' failed to start with new config")

        self._speakers[idx] = new_speaker
        self._config.speakers[idx] = new_config
        self._save_config()

        return new_speaker.get_status()

    async def _on_remove_speaker(self, speaker_id: str) -> None:
        """Remove a speaker at runtime."""
        idx = None
        for i, s in enumerate(self._speakers):
            if slugify_name(s.name) == speaker_id:
                idx = i
                break
        if idx is None:
            raise KeyError(speaker_id)

        speaker = self._speakers.pop(idx)
        self._config.speakers.pop(idx)

        await speaker.stop()
        self._save_config()

    def _save_config(self) -> None:
        """Persist current config to YAML file."""
        if self._config.config_path:
            try:
                save_config(self._config, self._config.config_path)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")

    # ------------------------------------------------------------------
    # Web server
    # ------------------------------------------------------------------

    async def _start_web_server(self) -> None:
        """Create the shared aiohttp app and start listening."""
        self._web_app = web.Application()

        # Expose state for route handlers
        self._web_app["auth_state"] = self._auth_state
        self._web_app["get_speakers"] = lambda: [s.get_status() for s in self._speakers]
        self._web_app["version"] = __version__
        self._web_app["commit"] = __commit__
        self._web_app["http_port"] = self._config.server.http_port
        self._web_app["on_auth_token"] = self._on_auth_token
        self._web_app["on_logout"] = self._on_logout
        self._web_app["on_add_speaker"] = self._on_add_speaker
        self._web_app["on_edit_speaker"] = self._on_edit_speaker
        self._web_app["on_remove_speaker"] = self._on_remove_speaker
        self._web_app["local_audio_enabled"] = os.environ.get(
            "QOBUZPROXY_LOCAL_AUDIO_UI", ""
        ).lower() in ("true", "1", "yes")

        register_routes(self._web_app)

        self._web_runner = web.AppRunner(self._web_app, access_log=None)
        await self._web_runner.setup()
        self._web_site = web.TCPSite(
            self._web_runner,
            self._config.server.bind_address,
            self._config.server.http_port,
        )
        await self._web_site.start()
        logger.info(
            f"Web server listening on "
            f"{self._config.server.bind_address}:{self._config.server.http_port}"
        )

    async def _stop_web_server(self) -> None:
        """Shut down the shared aiohttp app."""
        if self._web_site:
            await self._web_site.stop()
        if self._web_runner:
            await self._web_runner.cleanup()

    # ------------------------------------------------------------------
    # Speaker lifecycle
    # ------------------------------------------------------------------

    async def _start_speakers(self) -> None:
        """Create and start Speaker instances from config."""
        assert self._api_client is not None

        if not self._config.speakers:
            port = self._config.server.http_port
            logger.info(f"No speakers configured — add speakers at http://localhost:{port}")
            return

        speakers = [
            Speaker(
                config=sc,
                api_client=self._api_client,
                app_id=self._app_id,
            )
            for sc in self._config.speakers
        ]

        results = await asyncio.gather(*[s.start() for s in speakers], return_exceptions=True)

        started: list[Speaker] = []
        for speaker, result in zip(speakers, results):
            if isinstance(result, Exception):
                logger.warning(f"Speaker '{speaker.name}' failed to start: {result}")
            elif result is False:
                logger.warning(f"Speaker '{speaker.name}' failed to start")
            else:
                started.append(speaker)

        if not started:
            logger.error("No speakers started successfully — check configuration and logs")
            return

        self._speakers = started
        names = ", ".join(s.name for s in self._speakers)
        port = self._config.server.http_port
        logger.info(f"qobuz-proxy ready — {len(self._speakers)} speaker(s): {names}")
        logger.info(f"Web UI: http://localhost:{port}")

    async def _stop_speakers(self) -> None:
        """Stop all running speakers."""
        if self._speakers:
            await asyncio.gather(*[s.stop() for s in self._speakers], return_exceptions=True)
            self._speakers = []
