"""Tests for WebSocket manager."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

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
    cfg.qobuz.auth_token = "test_password"
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

    @pytest.mark.asyncio
    async def test_wait_for_valid_token_blocks_until_refresh(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """Expired tokens should wait for refresh instead of looping reconnects."""
        expired_tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="expired_jwt_token",
                exp=1,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        ws_manager.set_tokens(expired_tokens)
        ws_manager._should_run = True

        wait_task = asyncio.create_task(ws_manager._wait_for_valid_token(buffer_s=60))
        await asyncio.sleep(0)
        assert wait_task.done() is False

        ws_manager.set_tokens(valid_tokens)

        assert await asyncio.wait_for(wait_task, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_set_tokens_closes_existing_connection_for_refresh(
        self, ws_manager: WsManager, valid_tokens: ConnectTokens
    ) -> None:
        """Receiving fresh tokens while connected should force a reconnect."""
        ws_manager.set_tokens(valid_tokens)
        ws_manager._should_run = True
        ws_manager._ws = AsyncMock()

        refreshed_tokens = ConnectTokens(
            session_id=str(uuid.uuid4()),
            ws_token=JWTConnectToken(
                jwt="refreshed_jwt_token",
                exp=9999999999,
                endpoint="wss://test.qobuz.com/ws",
            ),
        )

        ws_manager.set_tokens(refreshed_tokens)
        await asyncio.sleep(0)

        ws_manager._ws.close.assert_awaited_once()


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


class TestMessageDropWhenDisconnected:
    """Messages produced while disconnected are dropped, never queued.

    Replaying stale frames on a fresh connection gets it killed by the
    server (error 1003 "Message gap too large"); everything we send is
    ephemeral state that is re-announced after reconnecting.
    """

    @pytest.mark.asyncio
    async def test_send_message_dropped_when_disconnected(self, ws_manager: WsManager) -> None:
        """Test that raw messages are dropped when not connected."""
        assert ws_manager.is_connected is False

        result = await ws_manager.send_message(b"test_message_data")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_state_update_dropped_when_disconnected(self, ws_manager: WsManager) -> None:
        """Test that state updates are dropped when disconnected."""
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

    @pytest.mark.asyncio
    async def test_dropped_message_does_not_consume_msg_ids(self, ws_manager: WsManager) -> None:
        """Dropped messages must not advance the codec counters.

        The server tolerates only small msgId gaps between consecutive
        messages on a connection, so ids consumed by never-sent messages
        would poison the sequence.
        """
        counter_before = ws_manager._codec._msg_counter

        await ws_manager.send_volume_changed(75)

        assert ws_manager._codec._msg_counter == counter_before


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
