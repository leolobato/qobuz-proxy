"""
Protocol buffer message encoding and decoding.

Handles the custom frame format used by Qobuz Connect WebSocket protocol.
"""

import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Tuple

from qobuz_proxy.proto import envelope as envelope_pb2
from qobuz_proxy.proto import payload as payload_pb2
from qobuz_proxy.proto import common as common_pb2

logger = logging.getLogger(__name__)


class MessageType(IntEnum):
    """Outer envelope message types."""

    AUTHENTICATE = 1
    SUBSCRIBE = 2
    UNSUBSCRIBE = 3
    PAYLOAD = 6
    ERROR = 9
    DISCONNECT = 10


class QConnectProto(IntEnum):
    """Protocol identifiers."""

    QP_QCONNECT = 1


class QConnectMessageType(IntEnum):
    """Inner QConnect message types (subset of commonly used)."""

    # Renderer -> Server
    RNDR_SRVR_JOIN_SESSION = 21
    RNDR_SRVR_DEVICE_INFO_UPDATED = 22
    RNDR_SRVR_STATE_UPDATED = 23
    RNDR_SRVR_RENDERER_ACTION = 24
    RNDR_SRVR_VOLUME_CHANGED = 25
    RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED = 26
    RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED = 27
    RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED = 28

    # Server -> Renderer commands
    SRVR_RNDR_SET_STATE = 41
    SRVR_RNDR_SET_VOLUME = 42
    SRVR_RNDR_SET_ACTIVE = 43
    SRVR_RNDR_SET_MAX_AUDIO_QUALITY = 44
    SRVR_RNDR_SET_LOOP_MODE = 45
    SRVR_RNDR_SET_SHUFFLE_MODE = 46
    SRVR_RNDR_SET_AUTOPLAY_MODE = 47

    # Server -> Controller
    SRVR_CTRL_QUEUE_STATE = 90
    SRVR_CTRL_QUEUE_TRACKS_LOADED = 91


@dataclass
class DecodedMessage:
    """Decoded WebSocket message."""

    msg_type: MessageType
    msg_id: int = 0
    msg_date: int = 0
    payload: Optional[bytes] = None
    error_code: int = 0
    error_message: str = ""


# Quality ID to protocol value mapping
# Qobuz quality IDs: 5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k
# Protocol values: 1=MP3, 2=LOSSLESS, 3=HIRES_L1, 4=HIRES_L3
QUALITY_TO_PROTOCOL = {5: 1, 6: 2, 7: 3, 27: 4}
PROTOCOL_TO_QUALITY = {1: 5, 2: 6, 3: 7, 4: 27}

# Default audio properties for each quality level: (sample_rate_hz, bit_depth, nb_channels)
QUALITY_AUDIO_PROPERTIES = {
    5: (44100, 16, 2),   # MP3 320kbps
    6: (44100, 16, 2),   # CD quality FLAC
    7: (96000, 24, 2),   # Hi-Res 96kHz
    27: (192000, 24, 2), # Hi-Res 192kHz
}


class ProtocolCodec:
    """
    Encodes and decodes Qobuz Connect protocol messages.

    Frame format: [msg_type: 1 byte][length: varint][payload: N bytes]
    """

    def __init__(self, device_uuid: bytes):
        """
        Initialize codec.

        Args:
            device_uuid: 16-byte device UUID
        """
        self.device_uuid = device_uuid
        self._msg_counter = 0

    def _next_msg_id(self) -> int:
        """Get next message ID."""
        self._msg_counter += 1
        return self._msg_counter

    def _now_ms(self) -> int:
        """Current time in milliseconds."""
        return int(time.time() * 1000)

    # -------------------------------------------------------------------------
    # Encoding
    # -------------------------------------------------------------------------

    def encode_authenticate(self, jwt: str) -> bytes:
        """
        Encode AUTHENTICATE message.

        Args:
            jwt: WebSocket JWT token

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Authenticate()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.jwt = jwt

        return self._pack_frame(MessageType.AUTHENTICATE, msg.SerializeToString())

    def encode_subscribe(self, session_uuid: bytes) -> bytes:
        """
        Encode SUBSCRIBE message.

        Args:
            session_uuid: 16-byte session UUID to subscribe to

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Subscribe()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.proto = QConnectProto.QP_QCONNECT
        msg.channels.append(session_uuid)

        return self._pack_frame(MessageType.SUBSCRIBE, msg.SerializeToString())

    def encode_payload(
        self,
        inner_payload: bytes,
        dest_channels: Optional[list[bytes]] = None,
    ) -> bytes:
        """
        Encode PAYLOAD message.

        Args:
            inner_payload: Serialized QConnectBatch
            dest_channels: Optional destination channel UUIDs

        Returns:
            Encoded frame bytes
        """
        msg = envelope_pb2.Payload()
        msg.msgId = self._next_msg_id()
        msg.msgDate = self._now_ms()
        msg.proto = QConnectProto.QP_QCONNECT
        msg.src = self.device_uuid
        if dest_channels:
            msg.dests.extend(dest_channels)
        msg.payload = inner_payload

        return self._pack_frame(MessageType.PAYLOAD, msg.SerializeToString())

    def encode_state_update(
        self,
        playing_state: int,
        buffer_state: int,
        position_ms: int,
        duration_ms: int,
        queue_item_id: int,
        queue_version_major: int,
        queue_version_minor: int,
    ) -> bytes:
        """
        Encode renderer state update message.

        Returns:
            Encoded frame bytes ready to send
        """
        # Build inner QueueRendererState
        state = common_pb2.QueueRendererState()
        state.playingState = playing_state
        state.bufferState = buffer_state

        position = common_pb2.Position()
        position.timestamp = self._now_ms()
        position.value = position_ms
        state.currentPosition.CopyFrom(position)

        state.duration = duration_ms
        state.currentQueueItemId = queue_item_id

        queue_ver = common_pb2.QueueVersion()
        queue_ver.major = queue_version_major
        queue_ver.minor = queue_version_minor
        state.queueVersion.CopyFrom(queue_ver)

        # Wrap in RndrSrvrStateUpdated
        state_updated = payload_pb2.RndrSrvrStateUpdated()
        state_updated.state.CopyFrom(state)

        # Wrap in QConnectMessage
        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_STATE_UPDATED
        qc_msg.rndrSrvrStateUpdated.CopyFrom(state_updated)

        # Wrap in QConnectBatch
        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def encode_join_session(
        self,
        device_uuid: bytes,
        friendly_name: str,
        session_uuid: bytes,
        initial_state: Optional[common_pb2.RendererState] = None,
        max_audio_quality: int = 27,
    ) -> bytes:
        """
        Encode join session message (sent when connecting).

        Args:
            device_uuid: 16-byte device UUID
            friendly_name: Device display name
            session_uuid: 16-byte session UUID to join
            initial_state: Optional initial renderer state
            max_audio_quality: Max quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)

        Returns:
            Encoded frame bytes
        """
        # Build DeviceInfo
        device_info = common_pb2.DeviceInfo()
        device_info.deviceUuid = device_uuid
        device_info.friendlyName = friendly_name
        device_info.brand = "QobuzProxy"
        device_info.model = "Python"
        device_info.type = common_pb2.DEVICE_TYPE_SPEAKER
        device_info.softwareVersion = "py-1.0.0"

        # Device capabilities - map quality ID to protocol value
        proto_quality = QUALITY_TO_PROTOCOL.get(max_audio_quality, 4)
        caps = common_pb2.DeviceCapabilities()
        caps.minAudioQuality = 1
        caps.maxAudioQuality = proto_quality
        caps.volumeRemoteControl = 2  # CONTROLLER
        device_info.capabilities.CopyFrom(caps)

        # Build JoinSession message
        join = payload_pb2.RndrSrvrJoinSession()
        join.sessionUuid = session_uuid  # Required!
        join.deviceInfo.CopyFrom(device_info)
        join.reason = 1  # Normal join
        join.isActive = True

        if initial_state:
            join.initialState.CopyFrom(initial_state)

        # Wrap in QConnectMessage
        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_JOIN_SESSION
        qc_msg.rndrSrvrJoinSession.CopyFrom(join)

        # Wrap in QConnectBatch
        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def encode_volume_changed(self, volume: int) -> bytes:
        """
        Encode volume changed notification.

        Args:
            volume: Volume level 0-100

        Returns:
            Encoded frame bytes
        """
        vol_msg = payload_pb2.RndrSrvrVolumeChanged()
        vol_msg.volume = volume

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_VOLUME_CHANGED
        qc_msg.rndrSrvrVolumeChanged.CopyFrom(vol_msg)

        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def encode_file_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bytes:
        """
        Encode file audio quality changed notification.

        This reports the quality of the currently playing file.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Sample rate in Hz (e.g. 44100, 96000). 0 = derive from quality.
            bit_depth: Bit depth (16 or 24). 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            Encoded frame bytes
        """
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)
        defaults = QUALITY_AUDIO_PROPERTIES.get(quality, (44100, 16, 2))
        sampling_rate = sampling_rate or defaults[0]
        bit_depth = bit_depth or defaults[1]
        nb_channels = nb_channels or defaults[2]

        quality_msg = payload_pb2.RndrSrvrFileAudioQualityChanged()
        quality_msg.sampling_rate = sampling_rate
        quality_msg.bit_depth = bit_depth
        quality_msg.nb_channels = nb_channels
        quality_msg.audio_quality = proto_quality

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrFileAudioQualityChanged.CopyFrom(quality_msg)

        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def encode_device_audio_quality_changed(
        self,
        quality: int,
        sampling_rate: int = 0,
        bit_depth: int = 0,
        nb_channels: int = 0,
    ) -> bytes:
        """
        Encode device audio quality changed notification.

        This reports the device's current max audio quality capability.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            sampling_rate: Max sample rate in Hz. 0 = derive from quality.
            bit_depth: Max bit depth. 0 = derive from quality.
            nb_channels: Number of channels. 0 = derive from quality.

        Returns:
            Encoded frame bytes
        """
        defaults = QUALITY_AUDIO_PROPERTIES.get(quality, (44100, 16, 2))
        sampling_rate = sampling_rate or defaults[0]
        bit_depth = bit_depth or defaults[1]
        nb_channels = nb_channels or defaults[2]

        quality_msg = payload_pb2.RndrSrvrDeviceAudioQualityChanged()
        quality_msg.sampling_rate = sampling_rate
        quality_msg.bit_depth = bit_depth
        quality_msg.nb_channels = nb_channels

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrDeviceAudioQualityChanged.CopyFrom(quality_msg)

        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def encode_max_audio_quality_changed(self, quality: int, network_type: int = 1) -> bytes:
        """
        Encode max audio quality changed notification.

        This reports when the user changes the quality setting.

        Args:
            quality: Quality ID (5=MP3, 6=CD, 7=Hi-Res 96k, 27=Hi-Res 192k)
            network_type: Network type (1=WiFi)

        Returns:
            Encoded frame bytes
        """
        if quality not in QUALITY_TO_PROTOCOL:
            logger.warning(
                f"Unknown quality {quality} for MAX_AUDIO_QUALITY_CHANGED, defaulting to Hi-Res 192k"
            )
        proto_quality = QUALITY_TO_PROTOCOL.get(quality, 4)

        quality_msg = payload_pb2.RndrSrvrMaxAudioQualityChanged()
        quality_msg.audio_quality = proto_quality
        quality_msg.network_type = network_type

        qc_msg = payload_pb2.QConnectMessage()
        qc_msg.messageType = QConnectMessageType.RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED
        qc_msg.rndrSrvrMaxAudioQualityChanged.CopyFrom(quality_msg)

        batch = payload_pb2.QConnectBatch()
        batch.messagesTime = self._now_ms()
        batch.messagesId = self._next_msg_id()
        batch.messages.append(qc_msg)

        return self.encode_payload(batch.SerializeToString())

    def _pack_frame(self, msg_type: MessageType, data: bytes) -> bytes:
        """
        Pack data into wire frame format.

        Format: [type: 1 byte][length: varint][data: N bytes]
        """
        frame = bytearray()
        frame.append(msg_type)

        # Encode length as varint
        length = len(data)
        while length > 0x7F:
            frame.append((length & 0x7F) | 0x80)
            length >>= 7
        frame.append(length & 0x7F)

        frame.extend(data)
        return bytes(frame)

    # -------------------------------------------------------------------------
    # Decoding
    # -------------------------------------------------------------------------

    def decode_frame(self, data: bytes) -> Optional[DecodedMessage]:
        """
        Decode a WebSocket frame.

        Args:
            data: Raw frame bytes

        Returns:
            DecodedMessage or None if invalid
        """
        if len(data) < 2:
            return None

        try:
            msg_type = MessageType(data[0])
        except ValueError:
            logger.warning(f"Unknown message type: {data[0]}")
            return None

        # Decode varint length
        length, offset = self._decode_varint(data, 1)
        if offset < 0:
            return None

        payload = data[offset : offset + length]

        return self._decode_by_type(msg_type, payload)

    def _decode_varint(self, data: bytes, start: int) -> Tuple[int, int]:
        """
        Decode varint starting at position.

        Returns:
            (value, next_offset) or (-1, -1) on error
        """
        value = 0
        shift = 0
        offset = start

        while offset < len(data):
            byte = data[offset]
            value |= (byte & 0x7F) << shift
            offset += 1
            if not (byte & 0x80):
                return value, offset
            shift += 7

        return -1, -1

    def _decode_by_type(self, msg_type: MessageType, payload: bytes) -> DecodedMessage:
        """Decode payload based on message type."""
        result = DecodedMessage(msg_type=msg_type)

        try:
            if msg_type == MessageType.PAYLOAD:
                msg = envelope_pb2.Payload()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId
                result.msg_date = msg.msgDate
                result.payload = msg.payload

            elif msg_type == MessageType.ERROR:
                msg = envelope_pb2.Error()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId
                result.error_code = msg.code
                result.error_message = msg.message

            elif msg_type == MessageType.DISCONNECT:
                msg = envelope_pb2.Disconnect()
                msg.ParseFromString(payload)
                result.msg_id = msg.msgId

        except Exception as e:
            logger.error(f"Failed to decode {msg_type.name}: {e}")

        return result

    def decode_qconnect_batch(self, payload: bytes) -> Optional[payload_pb2.QConnectBatch]:
        """
        Decode inner QConnectBatch from PAYLOAD message.

        Args:
            payload: Inner payload bytes

        Returns:
            QConnectBatch or None
        """
        try:
            batch = payload_pb2.QConnectBatch()
            batch.ParseFromString(payload)
            return batch
        except Exception as e:
            logger.error(f"Failed to decode QConnectBatch: {e}")
            return None
