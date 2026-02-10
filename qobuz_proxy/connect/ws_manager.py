"""
WebSocket connection manager.

Handles connection lifecycle, authentication, and message routing.
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, Optional

import websockets
from websockets import ClientConnection

from qobuz_proxy.auth.tokens import WSToken
from qobuz_proxy.config import Config

from .protocol import DecodedMessage, MessageType, ProtocolCodec
from .types import ConnectTokens

logger = logging.getLogger(__name__)

# Connection constants
PING_INTERVAL = 10.0  # seconds
PONG_TIMEOUT = 30.0  # seconds
RECV_TIMEOUT = 1.0  # seconds (for periodic checks)
TOKEN_REFRESH_BUFFER = 60  # seconds before expiry
INITIAL_RECONNECT_DELAY = 1.0  # seconds
MAX_RECONNECT_DELAY = 60.0  # seconds
RECONNECT_BACKOFF_MULTIPLIER = 2.0

# Message handler callback type
MessageHandler = Callable[[int, Any], None]


class WsManager:
    """
    Manages WebSocket connection to Qobuz servers.

    Handles:
    - Connection establishment and authentication
    - Automatic reconnection with exponential backoff
    - Message encoding/decoding via ProtocolCodec
    - Routing incoming messages to registered handlers
    """

    def __init__(self, config: Config):
        """
        Initialize WebSocket manager.

        Args:
            config: Application configuration
        """
        self.config = config
        self._device_uuid = self._uuid_to_bytes(config.device.uuid)
        self._codec = ProtocolCodec(self._device_uuid)

        # Connection state
        self._ws: Optional[ClientConnection] = None
        self._ws_token: Optional[WSToken] = None
        self._session_uuid: Optional[bytes] = None
        self._is_connected = False
        self._should_run = False

        # Reconnection state
        self._reconnect_delay = INITIAL_RECONNECT_DELAY

        # Message handlers: message_type -> handler
        self._handlers: Dict[int, MessageHandler] = {}

        # Outgoing message queue (for messages during disconnect)
        self._pending_messages: list[bytes] = []

        # Tasks
        self._receive_task: Optional[asyncio.Task[None]] = None

        # Callbacks
        self._on_connected: Optional[Callable[[], None]] = None
        self._on_disconnected: Optional[Callable[[], None]] = None

        # Quality setting for join session message
        self._max_audio_quality: int = 27  # Default to Hi-Res 192k

    def set_tokens(self, tokens: ConnectTokens) -> None:
        """
        Set connection tokens from discovery service.

        Args:
            tokens: Tokens received from Qobuz app
        """
        if tokens.ws_token:
            self._ws_token = WSToken.from_connect_token(
                jwt=tokens.ws_token.jwt,
                exp=tokens.ws_token.exp,
                endpoint=tokens.ws_token.endpoint,
            )
        self._session_uuid = self._uuid_to_bytes(tokens.session_id)
        logger.debug(f"Tokens set, endpoint: {self._ws_token.endpoint[:50]}...")

    def set_max_audio_quality(self, quality: int) -> None:
        """
        Set max audio quality for join session message.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
        """
        self._max_audio_quality = quality

    def on_connected(self, callback: Callable[[], None]) -> None:
        """Register callback for successful connection."""
        self._on_connected = callback

    def on_disconnected(self, callback: Callable[[], None]) -> None:
        """Register callback for disconnection."""
        self._on_disconnected = callback

    def register_handler(self, message_type: int, handler: MessageHandler) -> None:
        """
        Register a handler for a specific QConnect message type.

        Args:
            message_type: QConnectMessage type code (e.g., 41 for SET_STATE)
            handler: Callback function(message_type, message_data)
        """
        self._handlers[message_type] = handler
        logger.debug(f"Registered handler for message type {message_type}")

    async def start(self) -> None:
        """Start WebSocket connection loop."""
        if not self._ws_token or not self._ws_token.is_valid():
            logger.error("Cannot start WsManager: no valid tokens")
            return

        self._should_run = True
        self._receive_task = asyncio.create_task(self._connection_loop())
        logger.info("WebSocket manager started")

    async def stop(self) -> None:
        """Stop WebSocket connection."""
        self._should_run = False
        if self._ws:
            await self._ws.close()
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket manager stopped")

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._is_connected

    async def send_message(self, data: bytes) -> bool:
        """
        Send a pre-encoded message.

        Args:
            data: Encoded frame bytes

        Returns:
            True if sent, False if queued for later
        """
        if self._ws and self._is_connected:
            try:
                await self._ws.send(data)
                return True
            except Exception as e:
                logger.error(f"Failed to send message: {e}")

        # Queue for when connection is restored
        self._pending_messages.append(data)
        return False

    async def send_state_update(
        self,
        playing_state: int,
        buffer_state: int,
        position_ms: int,
        duration_ms: int,
        queue_item_id: int,
        queue_version_major: int,
        queue_version_minor: int,
    ) -> bool:
        """
        Send renderer state update.

        Returns:
            True if sent successfully
        """
        data = self._codec.encode_state_update(
            playing_state=playing_state,
            buffer_state=buffer_state,
            position_ms=position_ms,
            duration_ms=duration_ms,
            queue_item_id=queue_item_id,
            queue_version_major=queue_version_major,
            queue_version_minor=queue_version_minor,
        )
        return await self.send_message(data)

    async def send_volume_changed(self, volume: int) -> bool:
        """
        Send volume changed notification.

        Args:
            volume: Volume level 0-100

        Returns:
            True if sent successfully
        """
        data = self._codec.encode_volume_changed(volume)
        return await self.send_message(data)

    async def send_file_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bool:
        """
        Send file audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Sample rate in Hz (e.g. 44100, 96000). 0 = derive from quality.
            bit_depth: Bit depth (16 or 24). 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            True if sent successfully
        """
        from qobuz_proxy.connect.protocol import QUALITY_TO_PROTOCOL

        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(
            f"Sending FILE_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}, "
            f"sr={sampling_rate}, bd={bit_depth}, ch={nb_channels}"
        )
        data = self._codec.encode_file_audio_quality_changed(
            quality, sampling_rate=sampling_rate, bit_depth=bit_depth, nb_channels=nb_channels,
        )
        return await self.send_message(data)

    async def send_device_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bool:
        """
        Send device audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Max sample rate in Hz. 0 = derive from quality.
            bit_depth: Max bit depth. 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            True if sent successfully
        """
        from qobuz_proxy.connect.protocol import QUALITY_TO_PROTOCOL

        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(
            f"Sending DEVICE_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}, "
            f"sr={sampling_rate}, bd={bit_depth}, ch={nb_channels}"
        )
        data = self._codec.encode_device_audio_quality_changed(
            quality, sampling_rate=sampling_rate, bit_depth=bit_depth, nb_channels=nb_channels,
        )
        return await self.send_message(data)

    async def send_max_audio_quality_changed(self, quality: int, network_type: int = 1) -> bool:
        """
        Send max audio quality changed notification.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            network_type: Network type (1=WiFi)

        Returns:
            True if sent successfully
        """
        from qobuz_proxy.connect.protocol import QUALITY_TO_PROTOCOL

        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        logger.debug(f"Sending MAX_AUDIO_QUALITY_CHANGED: qobuz={quality} -> proto={proto_quality}")
        data = self._codec.encode_max_audio_quality_changed(quality, network_type=network_type)
        return await self.send_message(data)

    # -------------------------------------------------------------------------
    # Connection Loop
    # -------------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Main connection loop with reconnection logic."""
        while self._should_run:
            try:
                await self._connect_and_run()
            except Exception as e:
                logger.error(f"Connection error: {e}")

            if not self._should_run:
                break

            # Notify disconnection
            if self._on_disconnected:
                self._on_disconnected()

            # Exponential backoff
            logger.info(f"Reconnecting in {self._reconnect_delay:.1f}s...")
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * RECONNECT_BACKOFF_MULTIPLIER,
                MAX_RECONNECT_DELAY,
            )

    async def _connect_and_run(self) -> None:
        """Connect, authenticate, and handle messages."""
        if not self._ws_token or self._ws_token.is_expired():
            logger.error("Token expired, cannot connect")
            return

        endpoint = self._ws_token.endpoint
        logger.info(f"Connecting to {endpoint[:50]}...")

        try:
            async with websockets.connect(
                endpoint,
                origin="https://play.qobuz.com",
                subprotocols=["qws"],
                ping_interval=PING_INTERVAL,
                ping_timeout=PONG_TIMEOUT,
            ) as ws:
                self._ws = ws

                # Authenticate
                if not await self._authenticate():
                    return

                # Subscribe to session
                if not await self._subscribe():
                    return

                # Send join session message
                await self._send_join_session()

                self._is_connected = True
                self._reconnect_delay = INITIAL_RECONNECT_DELAY  # Reset backoff
                logger.info("Connected and authenticated")

                # Notify connected callback
                if self._on_connected:
                    self._on_connected()

                # Send any queued messages
                await self._flush_pending_messages()

                # Message receive loop
                await self._receive_loop()

        except websockets.ConnectionClosed as e:
            logger.warning(f"Connection closed: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Connection failed: {e}")
        finally:
            self._is_connected = False
            self._ws = None

    async def _authenticate(self) -> bool:
        """Send AUTHENTICATE message."""
        if not self._ws_token:
            return False
        auth_frame = self._codec.encode_authenticate(self._ws_token.jwt)
        await self._ws.send(auth_frame)
        logger.debug("Sent AUTHENTICATE")
        return True

    async def _subscribe(self) -> bool:
        """Send SUBSCRIBE message."""
        if not self._session_uuid:
            logger.error("No session UUID for subscribe")
            return False

        sub_frame = self._codec.encode_subscribe(self._session_uuid)
        await self._ws.send(sub_frame)
        logger.debug("Sent SUBSCRIBE")
        return True

    async def _send_join_session(self) -> None:
        """Send join session message to register as renderer."""
        if not self._session_uuid:
            logger.error("No session UUID for join session")
            return

        join_frame = self._codec.encode_join_session(
            device_uuid=self._device_uuid,
            friendly_name=self.config.device.name,
            session_uuid=self._session_uuid,
            max_audio_quality=self._max_audio_quality,
        )
        await self._ws.send(join_frame)
        logger.debug(f"Sent JOIN_SESSION with max_quality={self._max_audio_quality}")

    async def _receive_loop(self) -> None:
        """Receive and dispatch messages."""
        while self._should_run and self._ws:
            try:
                data = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=RECV_TIMEOUT,
                )
                await self._handle_message(data)

            except asyncio.TimeoutError:
                # Check token refresh
                if self._ws_token and self._ws_token.is_expired(TOKEN_REFRESH_BUFFER):
                    logger.warning("Token expiring soon, need refresh")
                    # Token refresh would be triggered here
                    # For now, we'll need to wait for new tokens from app
                    break

            except websockets.ConnectionClosed:
                raise

    async def _handle_message(self, data: bytes) -> None:
        """Decode and route incoming message."""
        decoded = self._codec.decode_frame(data)
        if not decoded:
            return

        if decoded.msg_type == MessageType.PAYLOAD:
            await self._handle_payload(decoded)
        elif decoded.msg_type == MessageType.ERROR:
            logger.error(f"Server error {decoded.error_code}: {decoded.error_message}")
        elif decoded.msg_type == MessageType.DISCONNECT:
            logger.warning("Server requested disconnect")
            raise websockets.ConnectionClosed(None, None)

    async def _handle_payload(self, decoded: DecodedMessage) -> None:
        """Handle PAYLOAD message by routing to registered handlers."""
        if not decoded.payload:
            return

        batch = self._codec.decode_qconnect_batch(decoded.payload)
        if not batch:
            return

        for msg in batch.messages:
            msg_type = msg.messageType
            handler = self._handlers.get(msg_type)
            if handler:
                try:
                    handler(msg_type, msg)
                except Exception as e:
                    logger.error(f"Handler error for type {msg_type}: {e}")
            else:
                logger.debug(f"No handler for message type {msg_type}")

    async def _flush_pending_messages(self) -> None:
        """Send any messages queued during disconnect."""
        if not self._pending_messages:
            return

        logger.debug(f"Flushing {len(self._pending_messages)} pending messages")
        for data in self._pending_messages:
            try:
                await self._ws.send(data)
            except Exception as e:
                logger.error(f"Failed to flush message: {e}")

        self._pending_messages.clear()

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    def _uuid_to_bytes(self, uuid_str: str) -> bytes:
        """Convert UUID string to 16 bytes."""
        try:
            return uuid.UUID(uuid_str).bytes
        except ValueError:
            # Fallback: hash the string
            import hashlib

            return hashlib.md5(uuid_str.encode()).digest()
