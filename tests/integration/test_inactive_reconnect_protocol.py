"""Portable #23 regression: run unchanged against v1.7.0 or current source."""

import uuid
from unittest.mock import AsyncMock

from qobuz_proxy.config import Config
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.types import ConnectTokens
from qobuz_proxy.connect.ws_manager import WsManager
from qobuz_proxy.proto import qconnect_payload_pb2 as pb


async def test_inactive_rejoin_does_not_request_session_ownership():
    manager = WsManager(Config())
    manager.set_tokens(ConnectTokens(session_id=str(uuid.uuid4())))
    manager._ws = AsyncMock()
    manager._is_connected = True
    batch = pb.QConnectBatch()
    message = batch.messages.add(messageType=43)
    message.srvrRndrSetActive.active = False
    await manager._handle_payload(
        DecodedMessage(
            msg_type=MessageType.PAYLOAD,
            payload=batch.SerializeToString(),
        )
    )
    await manager._send_join_session()
    decoded = manager._codec.decode_frame(manager._ws.send.call_args.args[0])
    join = manager._codec.decode_qconnect_batch(decoded.payload).messages[0].rndrSrvrJoinSession
    assert join.isActive is False, "An idle reconnect must not claim playback"
    assert join.reason == 2, "A reconnect must not masquerade as a controller selection"
