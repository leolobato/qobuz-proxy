"""Tests for WebSocket manager."""

import uuid
from unittest.mock import MagicMock

import pytest

from qobuz_proxy.config import Config
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.connect.ws_manager import (
    INITIAL_RECONNECT_DELAY,
    MAX_RECONNECT_DELAY,
    RECONNECT_BACKOFF_MULTIPLIER,
    WsManager,
)


@pytest.fixture
def config() -> Config:
    """Create a test configuration."""
    cfg = Config()
    cfg.qobuz.email = "test@test.com"
    cfg.qobuz.password = "test_password"
    cfg.device.name = "Test Device"
    cfg.device.uuid = str(uuid.uuid4())
    cfg.backend.dlna.ip = "192.168.1.100"
    return cfg


@pytest.fixture
def ws_manager(config: Config) -> WsManager:
    """Create a WsManager instance."""
    return WsManager(config)


@pytest.fixture
def valid_tokens() -> ConnectTokens:
    """Create valid test tokens."""
    return ConnectTokens(
        session_id=str(uuid.uuid4()),
        ws_token=JWTConnectToken(
            jwt="test_jwt_token",
            exp=9999999999,  # Far future
            endpoint="wss://test.qobuz.com/ws",
        ),
    )


class TestWsManagerInit:
    """Tests for WsManager initialization."""

    def test_init_creates_codec(self, ws_manager: WsManager) -> None:
        """Test that initialization creates a protocol codec."""
        assert ws_manager._codec is not None

    def test_init_not_connected(self, ws_manager: WsManager) -> None:
        """Test that manager starts disconnected."""
        assert ws_manager.is_connected is False

    def test_init_no_tokens(self, ws_manager: WsManager) -> None:
        """Test that manager starts without tokens."""
        assert ws_manager._ws_token is None
        assert ws_manager._session_uuid is None

    def test_init_empty_handlers(self, ws_manager: WsManager) -> None:
        """Test that handler dict is empty initially."""
        assert len(ws_manager._handlers) == 0

    def test_init_empty_pending_messages(self, ws_manager: WsManager) -> None:
        """Test that pending messages queue is empty."""
        assert len(ws_manager._pending_messages) == 0

    def test_init_reconnect_delay(self, ws_manager: WsManager) -> None:
        """Test initial reconnect delay."""
        assert ws_manager._reconnect_delay == INITIAL_RECONNECT_DELAY


class TestTokenManagement:
    """Tests for token management."""

    def test_set_tokens(self, ws_manager: WsManager, valid_tokens: ConnectTokens) -> None:
        """Test setting connection tokens."""
        ws_manager.set_tokens(valid_tokens)

        assert ws_manager._ws_token is not None
        assert ws_manager._ws_token.jwt == "test_jwt_token"
        assert ws_manager._ws_token.endpoint == "wss://test.qobuz.com/ws"
        assert ws_manager._session_uuid is not None

    def test_set_tokens_converts_session_uuid(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """Test that session ID is converted to bytes."""
        ws_manager.set_tokens(valid_tokens)
        assert isinstance(ws_manager._session_uuid, bytes)
        assert len(ws_manager._session_uuid) == 16


class TestHandlerRegistration:
    """Tests for message handler registration."""

    def test_register_handler(self, ws_manager: WsManager) -> None:
        """Test registering a message handler."""
        handler = MagicMock()
        ws_manager.register_handler(41, handler)  # SET_STATE

        assert 41 in ws_manager._handlers
        assert ws_manager._handlers[41] is handler

    def test_register_multiple_handlers(self, ws_manager: WsManager) -> None:
        """Test registering multiple handlers."""
        handler1 = MagicMock()
        handler2 = MagicMock()
        handler3 = MagicMock()

        ws_manager.register_handler(41, handler1)
        ws_manager.register_handler(42, handler2)
        ws_manager.register_handler(43, handler3)

        assert len(ws_manager._handlers) == 3

    def test_register_handler_overwrites(self, ws_manager: WsManager) -> None:
        """Test that registering same type overwrites."""
        handler1 = MagicMock()
        handler2 = MagicMock()

        ws_manager.register_handler(41, handler1)
        ws_manager.register_handler(41, handler2)

        assert ws_manager._handlers[41] is handler2


class TestCallbacks:
    """Tests for connection callbacks."""

    def test_on_connected_callback(self, ws_manager: WsManager) -> None:
        """Test setting connected callback."""
        callback = MagicMock()
        ws_manager.on_connected(callback)
        assert ws_manager._on_connected is callback

    def test_on_disconnected_callback(self, ws_manager: WsManager) -> None:
        """Test setting disconnected callback."""
        callback = MagicMock()
        ws_manager.on_disconnected(callback)
        assert ws_manager._on_disconnected is callback


class TestUuidConversion:
    """Tests for UUID conversion utility."""

    def test_uuid_to_bytes_valid_uuid(self, ws_manager: WsManager) -> None:
        """Test converting valid UUID string to bytes."""
        uuid_str = "12345678-1234-5678-1234-567812345678"
        result = ws_manager._uuid_to_bytes(uuid_str)

        assert isinstance(result, bytes)
        assert len(result) == 16

    def test_uuid_to_bytes_invalid_uuid_uses_hash(self, ws_manager: WsManager) -> None:
        """Test that invalid UUID falls back to hash."""
        invalid_uuid = "not-a-valid-uuid"
        result = ws_manager._uuid_to_bytes(invalid_uuid)

        assert isinstance(result, bytes)
        assert len(result) == 16  # MD5 hash is 16 bytes


class TestReconnectionConstants:
    """Tests for reconnection constants."""

    def test_initial_delay(self) -> None:
        """Test initial reconnect delay value."""
        assert INITIAL_RECONNECT_DELAY == 1.0

    def test_max_delay(self) -> None:
        """Test maximum reconnect delay value."""
        assert MAX_RECONNECT_DELAY == 60.0

    def test_backoff_multiplier(self) -> None:
        """Test backoff multiplier value."""
        assert RECONNECT_BACKOFF_MULTIPLIER == 2.0

    def test_backoff_sequence(self) -> None:
        """Test expected backoff sequence."""
        delay = INITIAL_RECONNECT_DELAY
        expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

        for expected_delay in expected:
            assert delay == expected_delay
            delay = min(delay * RECONNECT_BACKOFF_MULTIPLIER, MAX_RECONNECT_DELAY)


class TestMessageQueueing:
    """Tests for message queueing during disconnect."""

    @pytest.mark.asyncio
    async def test_send_message_queues_when_disconnected(self, ws_manager: WsManager) -> None:
        """Test that messages are queued when not connected."""
        assert ws_manager.is_connected is False

        data = b"test_message_data"
        result = await ws_manager.send_message(data)

        assert result is False
        assert len(ws_manager._pending_messages) == 1
        assert ws_manager._pending_messages[0] == data

    @pytest.mark.asyncio
    async def test_multiple_messages_queued(self, ws_manager: WsManager) -> None:
        """Test that multiple messages can be queued."""
        messages = [b"msg1", b"msg2", b"msg3"]

        for msg in messages:
            await ws_manager.send_message(msg)

        assert len(ws_manager._pending_messages) == 3

    @pytest.mark.asyncio
    async def test_send_state_update_queues(self, ws_manager: WsManager) -> None:
        """Test that state updates are queued when disconnected."""
        result = await ws_manager.send_state_update(
            playing_state=2,
            buffer_state=2,
            position_ms=1000,
            duration_ms=60000,
            queue_item_id=1,
            queue_version_major=1,
            queue_version_minor=0,
        )

        assert result is False
        assert len(ws_manager._pending_messages) == 1

    @pytest.mark.asyncio
    async def test_send_volume_changed_queues(self, ws_manager: WsManager) -> None:
        """Test that volume changes are queued when disconnected."""
        result = await ws_manager.send_volume_changed(75)

        assert result is False
        assert len(ws_manager._pending_messages) == 1


class TestStartStop:
    """Tests for start/stop behavior."""

    @pytest.mark.asyncio
    async def test_start_without_tokens_logs_error(self, ws_manager: WsManager) -> None:
        """Test that starting without tokens doesn't crash."""
        # Should log error and return, not raise
        await ws_manager.start()
        # Manager should not be running
        assert ws_manager._should_run is False or ws_manager._receive_task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, ws_manager: WsManager) -> None:
        """Test that stopping when not running is safe."""
        await ws_manager.stop()
        assert ws_manager._should_run is False
