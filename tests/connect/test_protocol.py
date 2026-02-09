"""Tests for protocol encoding/decoding."""

import uuid

import pytest

from qobuz_proxy.connect.protocol import (
    MessageType,
    ProtocolCodec,
    QConnectMessageType,
)


@pytest.fixture
def device_uuid() -> bytes:
    """Generate a test device UUID."""
    return uuid.uuid4().bytes


@pytest.fixture
def codec(device_uuid: bytes) -> ProtocolCodec:
    """Create a ProtocolCodec instance."""
    return ProtocolCodec(device_uuid)


class TestProtocolCodec:
    """Tests for ProtocolCodec class."""

    def test_init(self, device_uuid: bytes) -> None:
        """Test codec initialization."""
        codec = ProtocolCodec(device_uuid)
        assert codec.device_uuid == device_uuid
        assert codec._msg_counter == 0

    def test_next_msg_id_increments(self, codec: ProtocolCodec) -> None:
        """Test message ID counter increments."""
        id1 = codec._next_msg_id()
        id2 = codec._next_msg_id()
        id3 = codec._next_msg_id()
        assert id1 == 1
        assert id2 == 2
        assert id3 == 3


class TestEncoding:
    """Tests for message encoding."""

    def test_encode_authenticate(self, codec: ProtocolCodec) -> None:
        """Test AUTHENTICATE message encoding."""
        jwt = "test_jwt_token_123"
        frame = codec.encode_authenticate(jwt)

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.AUTHENTICATE

    def test_encode_subscribe(self, codec: ProtocolCodec) -> None:
        """Test SUBSCRIBE message encoding."""
        session_uuid = uuid.uuid4().bytes
        frame = codec.encode_subscribe(session_uuid)

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.SUBSCRIBE

    def test_encode_payload(self, codec: ProtocolCodec) -> None:
        """Test PAYLOAD message encoding."""
        inner_payload = b"test_payload_data"
        frame = codec.encode_payload(inner_payload)

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_payload_with_destinations(self, codec: ProtocolCodec) -> None:
        """Test PAYLOAD with destination channels."""
        inner_payload = b"test_payload"
        dest = [uuid.uuid4().bytes, uuid.uuid4().bytes]
        frame = codec.encode_payload(inner_payload, dest_channels=dest)

        assert isinstance(frame, bytes)
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_state_update(self, codec: ProtocolCodec) -> None:
        """Test state update message encoding."""
        frame = codec.encode_state_update(
            playing_state=2,  # PLAYING
            buffer_state=2,  # OK
            position_ms=5000,
            duration_ms=180000,
            queue_item_id=42,
            queue_version_major=1,
            queue_version_minor=5,
        )

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_join_session(self, codec: ProtocolCodec, device_uuid: bytes) -> None:
        """Test join session message encoding."""
        session_uuid = uuid.UUID("11111111-2222-3333-4444-555555555555").bytes
        frame = codec.encode_join_session(
            device_uuid=device_uuid,
            friendly_name="Test Device",
            session_uuid=session_uuid,
        )

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_join_session_with_quality(
        self, codec: ProtocolCodec, device_uuid: bytes
    ) -> None:
        """Test join session message encoding with quality parameter."""
        session_uuid = uuid.UUID("11111111-2222-3333-4444-555555555555").bytes
        frame = codec.encode_join_session(
            device_uuid=device_uuid,
            friendly_name="Test Device",
            session_uuid=session_uuid,
            max_audio_quality=6,  # CD quality
        )

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_volume_changed(self, codec: ProtocolCodec) -> None:
        """Test volume changed message encoding."""
        frame = codec.encode_volume_changed(volume=75)

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_file_audio_quality_changed(self, codec: ProtocolCodec) -> None:
        """Test file audio quality changed message encoding."""
        frame = codec.encode_file_audio_quality_changed(quality=27)  # Hi-Res 192k

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_device_audio_quality_changed(self, codec: ProtocolCodec) -> None:
        """Test device audio quality changed message encoding."""
        frame = codec.encode_device_audio_quality_changed(quality=6)  # CD quality

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD

    def test_encode_max_audio_quality_changed(self, codec: ProtocolCodec) -> None:
        """Test max audio quality changed message encoding."""
        frame = codec.encode_max_audio_quality_changed(quality=7)  # Hi-Res 96k

        assert isinstance(frame, bytes)
        assert len(frame) > 0
        assert frame[0] == MessageType.PAYLOAD


class TestDecoding:
    """Tests for message decoding."""

    def test_decode_authenticate_frame(self, codec: ProtocolCodec) -> None:
        """Test decoding an AUTHENTICATE frame we encoded."""
        jwt = "test_jwt"
        frame = codec.encode_authenticate(jwt)
        decoded = codec.decode_frame(frame)

        assert decoded is not None
        assert decoded.msg_type == MessageType.AUTHENTICATE

    def test_decode_subscribe_frame(self, codec: ProtocolCodec) -> None:
        """Test decoding a SUBSCRIBE frame we encoded."""
        session_uuid = uuid.uuid4().bytes
        frame = codec.encode_subscribe(session_uuid)
        decoded = codec.decode_frame(frame)

        assert decoded is not None
        assert decoded.msg_type == MessageType.SUBSCRIBE

    def test_decode_payload_frame(self, codec: ProtocolCodec) -> None:
        """Test decoding a PAYLOAD frame we encoded."""
        frame = codec.encode_state_update(
            playing_state=2,
            buffer_state=2,
            position_ms=1000,
            duration_ms=60000,
            queue_item_id=1,
            queue_version_major=1,
            queue_version_minor=0,
        )
        decoded = codec.decode_frame(frame)

        assert decoded is not None
        assert decoded.msg_type == MessageType.PAYLOAD
        assert decoded.payload is not None

    def test_decode_empty_data_returns_none(self, codec: ProtocolCodec) -> None:
        """Test that empty data returns None."""
        decoded = codec.decode_frame(b"")
        assert decoded is None

    def test_decode_single_byte_returns_none(self, codec: ProtocolCodec) -> None:
        """Test that single byte returns None."""
        decoded = codec.decode_frame(b"\x01")
        assert decoded is None

    def test_decode_unknown_type_returns_none(self, codec: ProtocolCodec) -> None:
        """Test that unknown message type returns None."""
        # Type 255 is not defined
        decoded = codec.decode_frame(b"\xff\x00")
        assert decoded is None

    def test_decode_qconnect_batch(self, codec: ProtocolCodec) -> None:
        """Test decoding QConnectBatch from payload."""
        # Encode a state update (creates a QConnectBatch)
        frame = codec.encode_state_update(
            playing_state=2,
            buffer_state=2,
            position_ms=1000,
            duration_ms=60000,
            queue_item_id=1,
            queue_version_major=1,
            queue_version_minor=0,
        )

        # Decode the frame
        decoded = codec.decode_frame(frame)
        assert decoded is not None
        assert decoded.payload is not None

        # Decode the inner batch
        batch = codec.decode_qconnect_batch(decoded.payload)
        assert batch is not None
        assert len(batch.messages) > 0
        assert batch.messages[0].messageType == QConnectMessageType.RNDR_SRVR_STATE_UPDATED


class TestVarintEncoding:
    """Tests for varint encoding/decoding."""

    def test_small_length(self, codec: ProtocolCodec) -> None:
        """Test encoding small payloads (length < 128)."""
        small_jwt = "x" * 50
        frame = codec.encode_authenticate(small_jwt)
        decoded = codec.decode_frame(frame)
        assert decoded is not None

    def test_medium_length(self, codec: ProtocolCodec) -> None:
        """Test encoding medium payloads (length 128-16383)."""
        medium_jwt = "x" * 200
        frame = codec.encode_authenticate(medium_jwt)
        decoded = codec.decode_frame(frame)
        assert decoded is not None

    def test_large_length(self, codec: ProtocolCodec) -> None:
        """Test encoding larger payloads."""
        large_jwt = "x" * 20000
        frame = codec.encode_authenticate(large_jwt)
        decoded = codec.decode_frame(frame)
        assert decoded is not None


class TestMessageTypes:
    """Tests for message type enums."""

    def test_message_type_values(self) -> None:
        """Test MessageType enum values match protocol."""
        assert MessageType.AUTHENTICATE == 1
        assert MessageType.SUBSCRIBE == 2
        assert MessageType.UNSUBSCRIBE == 3
        assert MessageType.PAYLOAD == 6
        assert MessageType.ERROR == 9
        assert MessageType.DISCONNECT == 10

    def test_qconnect_message_type_values(self) -> None:
        """Test QConnectMessageType values match protocol."""
        # Renderer -> Server messages
        assert QConnectMessageType.RNDR_SRVR_JOIN_SESSION == 21
        assert QConnectMessageType.RNDR_SRVR_STATE_UPDATED == 23
        assert QConnectMessageType.RNDR_SRVR_VOLUME_CHANGED == 25
        assert QConnectMessageType.RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED == 26
        assert QConnectMessageType.RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED == 27
        assert QConnectMessageType.RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED == 28
        # Server -> Renderer messages
        assert QConnectMessageType.SRVR_RNDR_SET_STATE == 41
        assert QConnectMessageType.SRVR_RNDR_SET_VOLUME == 42
