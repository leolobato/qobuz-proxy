"""
QobuzProxy Application.

Main orchestrator that wires together all components and manages lifecycle.
"""

import asyncio
import logging
import signal
from typing import Optional

from qobuz_proxy.config import Config, AUTO_QUALITY, AUTO_FALLBACK_QUALITY
from qobuz_proxy.auth import (
    QobuzAPIClient,
    AuthenticationError,
    auto_fetch_credentials,
)
from qobuz_proxy.connect import DiscoveryService, WsManager, ConnectTokens
from qobuz_proxy.playback import (
    MetadataService,
    QobuzQueue,
    QobuzPlayer,
    QueueHandler,
    PlaybackCommandHandler,
    VolumeCommandHandler,
    StateReporter,
)
from qobuz_proxy.backends import AudioBackend, BackendFactory
from qobuz_proxy.backends.dlna import (
    AudioProxyServer,
    DLNABackend,
    MetadataServiceURLProvider,
)

logger = logging.getLogger(__name__)


class QobuzProxy:
    """
    Main QobuzProxy application.

    Orchestrates all components:
    - Authentication (QobuzAPIClient)
    - Qobuz Connect protocol (DiscoveryService, WsManager)
    - Playback (MetadataService, QobuzQueue, QobuzPlayer)
    - Audio backend (DLNABackend, AudioProxyServer)

    Usage:
        config = load_config(...)
        app = QobuzProxy(config)
        await app.run()
    """

    def __init__(self, config: Config):
        """
        Initialize QobuzProxy.

        Args:
            config: Validated configuration
        """
        self._config = config
        self._is_running = False
        self._shutdown_event = asyncio.Event()
        self._ws_connected_event = asyncio.Event()

        # Components (initialized in start())
        self._api_client: Optional[QobuzAPIClient] = None
        self._discovery: Optional[DiscoveryService] = None
        self._ws_manager: Optional[WsManager] = None
        self._metadata_service: Optional[MetadataService] = None
        self._queue: Optional[QobuzQueue] = None
        self._player: Optional[QobuzPlayer] = None
        self._backend: Optional[AudioBackend] = None
        self._proxy_server: Optional[AudioProxyServer] = None
        self._state_reporter: Optional[StateReporter] = None

        # Handlers
        self._queue_handler: Optional[QueueHandler] = None
        self._playback_handler: Optional[PlaybackCommandHandler] = None
        self._volume_handler: Optional[VolumeCommandHandler] = None

        # Credentials
        self._app_id: str = ""
        self._app_secret: str = ""

        # Effective quality (may differ from config if auto-detected)
        self._effective_quality: int = 27  # Default to Hi-Res 192k

    async def start(self) -> None:
        """
        Start QobuzProxy and all components.

        Startup order:
        1. Fetch app credentials from Qobuz web player
        2. API client and user authentication
        3. Audio backend (DLNA)
        4. Audio proxy server
        5. Metadata service
        6. Queue and player
        7. Discovery service (mDNS + HTTP)
        8. Wait for Qobuz app to connect
        9. WebSocket connection

        Raises:
            AuthenticationError: If Qobuz login fails
            ConnectionError: If network setup fails
        """
        logger.info("Starting QobuzProxy...")

        # 1. Fetch app credentials
        logger.info("Fetching Qobuz app credentials...")
        credentials = await auto_fetch_credentials()
        if not credentials:
            raise AuthenticationError("Failed to fetch Qobuz app credentials")

        self._app_id = credentials["app_id"]
        self._app_secret = credentials["app_secret"]
        logger.debug(f"Got app_id: {self._app_id}")

        # 2. Initialize API client and authenticate
        logger.debug("Initializing API client...")
        self._api_client = QobuzAPIClient(self._app_id, self._app_secret)

        # Login
        logger.info(f"Authenticating as {self._config.qobuz.email}...")
        if not await self._api_client.login(
            email=self._config.qobuz.email,
            password=self._config.qobuz.password,
        ):
            raise AuthenticationError("Qobuz login failed - check credentials")
        logger.info("Authentication successful")

        # 3. Create audio backend
        logger.debug("Creating audio backend...")
        backend = await BackendFactory.create_from_config(self._config)
        self._backend = backend
        logger.info(f"Connected to backend: {backend.name}")

        # 4. Resolve effective quality (handle auto-detection)
        self._effective_quality = self._config.qobuz.max_quality
        if self._effective_quality == AUTO_QUALITY:
            if isinstance(backend, DLNABackend):
                recommended = backend.get_recommended_quality()
                if recommended:
                    self._effective_quality = recommended
                    quality_names = {
                        5: "MP3",
                        6: "CD (FLAC 16/44)",
                        7: "Hi-Res (24/96)",
                        27: "Hi-Res (24/192)",
                    }
                    logger.info(
                        f"Auto-detected max quality: {quality_names.get(self._effective_quality, self._effective_quality)}"
                    )
                else:
                    self._effective_quality = AUTO_FALLBACK_QUALITY
                    logger.info(
                        "Capability discovery unavailable, using fallback quality: CD (FLAC 16/44)"
                    )
            else:
                # Local backend: default to Hi-Res 192k
                self._effective_quality = 27
                logger.info("Local backend, using max quality: Hi-Res (24/192)")

        # 5. Create metadata service (needed by proxy server URL provider)
        logger.debug("Creating metadata service...")
        self._metadata_service = MetadataService(
            api_client=self._api_client,
            max_quality=self._effective_quality,
        )

        # 6. Create and start audio proxy server (DLNA only)
        if isinstance(backend, DLNABackend):
            logger.debug("Starting audio proxy server...")
            url_provider = MetadataServiceURLProvider(self._metadata_service)
            self._proxy_server = AudioProxyServer(
                url_provider=url_provider,
                host=self._config.server.bind_address,
                port=self._config.server.proxy_port,
            )
            await self._proxy_server.start()
            logger.info(
                f"Audio proxy listening on "
                f"{self._config.server.bind_address}:{self._config.server.proxy_port}"
            )
            backend.set_proxy_server(self._proxy_server)

        # 7. Create queue and player
        logger.debug("Creating queue and player...")
        self._queue = QobuzQueue()
        self._player = QobuzPlayer(
            queue=self._queue,
            metadata_service=self._metadata_service,
            backend=backend,
        )
        # Only set fixed volume for DLNA backends
        if isinstance(backend, DLNABackend):
            self._player.set_fixed_volume_mode(self._config.backend.dlna.fixed_volume)

        # 8. Create and start discovery service
        logger.debug("Starting discovery service...")
        self._discovery = DiscoveryService(
            config=self._config,
            app_id=self._app_id,
            on_connect=self._on_app_connected,
            quality_getter=self._get_effective_quality,
        )
        await self._discovery.start()
        logger.info(f"Discovery service started on port {self._config.server.http_port}")

        # Mark as running (partial - waiting for app connection)
        self._is_running = True
        logger.info(
            f"QobuzProxy ready - device '{self._config.device.name}' "
            f"is now visible in Qobuz app"
        )
        logger.info("Waiting for Qobuz app to connect...")

        # 9. Wait for Qobuz app to connect (with tokens)
        try:
            await asyncio.wait_for(
                self._ws_connected_event.wait(),
                timeout=None,  # Wait indefinitely
            )
        except asyncio.CancelledError:
            # Shutdown requested before app connected
            return

    def _get_effective_quality(self) -> int:
        """Get current effective quality setting."""
        return self._effective_quality

    def _on_app_connected(self, tokens: ConnectTokens) -> None:
        """Callback when Qobuz app connects with tokens."""
        logger.info("Qobuz app connected, setting up WebSocket...")

        # Schedule async initialization
        asyncio.create_task(self._setup_websocket(tokens))

    async def _setup_websocket(self, tokens: ConnectTokens) -> None:
        """Set up WebSocket connection after receiving tokens."""
        # These must be set before this method is called
        assert self._queue is not None
        assert self._player is not None

        try:
            # Create WebSocket manager
            self._ws_manager = WsManager(config=self._config)
            self._ws_manager.set_tokens(tokens)
            self._ws_manager.set_max_audio_quality(self._effective_quality)

            # Create handlers
            self._queue_handler = QueueHandler(self._queue)
            self._playback_handler = PlaybackCommandHandler(
                self._player,
                on_quality_change=self._on_quality_change,
            )
            self._volume_handler = VolumeCommandHandler(self._player)

            # Wire up next track callbacks for auto-advance
            self._player.set_next_track_callbacks(
                get_callback=self._playback_handler.get_next_track_info,
                clear_callback=self._playback_handler.clear_next_track_info,
            )

            # Register handlers for their message types
            for msg_type in self._queue_handler.get_message_types():
                self._ws_manager.register_handler(
                    msg_type,
                    lambda mt, msg, h=self._queue_handler: asyncio.create_task(
                        h.handle_message(mt, msg)
                    ),
                )

            for msg_type in self._playback_handler.get_message_types():
                self._ws_manager.register_handler(
                    msg_type,
                    lambda mt, msg, h=self._playback_handler: asyncio.create_task(
                        h.handle_message(mt, msg)
                    ),
                )

            for msg_type in self._volume_handler.get_message_types():
                self._ws_manager.register_handler(
                    msg_type,
                    lambda mt, msg, h=self._volume_handler: asyncio.create_task(
                        h.handle_message(mt, msg)
                    ),
                )

            # Register error handler (message type 1)
            self._ws_manager.register_handler(
                1,  # MESSAGE_TYPE_ERROR
                self._handle_protocol_error,
            )

            # Create and wire state reporter
            self._state_reporter = StateReporter(
                player=self._player,
                queue=self._queue,
                send_callback=self._send_state_report,
            )
            self._player.set_state_reporter(self._state_reporter)

            # Wire volume reporting
            self._player.set_volume_report_callback(self._ws_manager.send_volume_changed)

            # Wire file quality reporting (sends per-track quality info to app)
            self._player.set_file_quality_report_callback(
                self._ws_manager.send_file_audio_quality_changed
            )

            # Start WebSocket connection
            await self._ws_manager.start()
            logger.info("WebSocket connected to Qobuz servers")

            # Start state reporter
            await self._state_reporter.start()

            # Start player
            await self._player.start()
            logger.info("Player started")

            # Signal that connection is complete
            self._ws_connected_event.set()

        except Exception as e:
            logger.error(f"Failed to set up WebSocket: {e}", exc_info=True)
            # Don't signal - will cause eventual shutdown

    async def _on_quality_change(self, new_quality: int) -> None:
        """
        Handle quality change request from Qobuz app.

        Args:
            new_quality: New quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
        """
        if new_quality == self._effective_quality:
            logger.debug(f"Quality unchanged: {new_quality}")
            return

        logger.info(f"Quality changed: {self._effective_quality} -> {new_quality}")
        self._effective_quality = new_quality

        # Update metadata service (invalidates cached URLs)
        if self._metadata_service:
            self._metadata_service.set_max_quality(new_quality)

        # Note: Reference implementation does NOT send confirmation
        # The server handles notifying controllers

    def _handle_protocol_error(self, msg_type: int, msg) -> None:
        """Handle protocol error messages from server."""
        error = msg.error if msg.HasField("error") else None
        if error:
            logger.error(f"Protocol error: code={error.code}, message={error.message}")
        else:
            logger.error(f"Protocol error message received (type {msg_type})")

    async def _send_state_report(self, report) -> None:
        """Send state report via WebSocket."""
        if not self._ws_manager:
            return

        # Map internal state to protocol state
        # Protocol only supports: 1=STOPPED, 2=PLAYING, 3=PAUSED
        from qobuz_proxy.backends import PlaybackState

        playing_state = report.playing_state
        if playing_state == PlaybackState.LOADING:
            playing_state = PlaybackState.STOPPED
        elif playing_state == PlaybackState.ERROR:
            playing_state = PlaybackState.STOPPED

        await self._ws_manager.send_state_update(
            playing_state=int(playing_state),
            buffer_state=int(report.buffer_state),
            position_ms=report.position_value_ms,
            duration_ms=report.duration_ms,
            queue_item_id=report.current_queue_item_id,
            queue_version_major=report.queue_version_major,
            queue_version_minor=report.queue_version_minor,
        )

    async def stop(self) -> None:
        """
        Stop QobuzProxy and all components.

        Shutdown order (reverse of startup):
        1. Stop state reporter
        2. Stop player
        3. Disconnect WebSocket
        4. Stop discovery service
        5. Stop audio proxy
        6. Disconnect backend
        """
        if not self._is_running:
            return

        logger.info("Stopping QobuzProxy...")
        self._is_running = False

        # 1. Stop state reporter
        if self._state_reporter:
            try:
                await self._state_reporter.stop()
            except Exception as e:
                logger.warning(f"Error stopping state reporter: {e}")

        # 2. Stop player
        if self._player:
            try:
                await self._player.stop()
            except Exception as e:
                logger.warning(f"Error stopping player: {e}")

        # 3. Disconnect WebSocket
        if self._ws_manager:
            try:
                await self._ws_manager.stop()
            except Exception as e:
                logger.warning(f"Error disconnecting WebSocket: {e}")

        # 4. Stop discovery service
        if self._discovery:
            try:
                await self._discovery.stop()
            except Exception as e:
                logger.warning(f"Error stopping discovery service: {e}")

        # 5. Stop audio proxy
        if self._proxy_server:
            try:
                await self._proxy_server.stop()
            except Exception as e:
                logger.warning(f"Error stopping proxy server: {e}")

        # 6. Disconnect backend
        if self._backend:
            try:
                await self._backend.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting backend: {e}")

        logger.info("QobuzProxy stopped")

    async def run(self) -> None:
        """
        Run QobuzProxy until interrupted.

        Sets up signal handlers for graceful shutdown on SIGINT/SIGTERM.
        """
        # Setup signal handlers
        loop = asyncio.get_running_loop()

        def handle_signal() -> None:
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

        try:
            await self.start()

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        finally:
            await self.stop()

    @property
    def is_running(self) -> bool:
        """Check if the application is running."""
        return self._is_running
