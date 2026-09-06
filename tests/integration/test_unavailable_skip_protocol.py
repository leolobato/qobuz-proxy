"""Exercise #21 through the real protobuf, handler, queue, player and reporter.

The server accepts current-item reports but deliberately sends no unsolicited
SET_STATE. Only an explicit renderer NEXT advances its queue. The network and
audio/API boundaries are fakes; no Qobuz credentials or physical speakers needed.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.backends import PlaybackState
from qobuz_proxy.config import Config
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.ws_manager import WsManager
from qobuz_proxy.playback.command_handler import PlaybackCommandHandler
from qobuz_proxy.playback.queue import QobuzQueue
from qobuz_proxy.playback.state_reporter import StateReporter
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from tests.playback.test_unavailable_track_skip import _setup


class Session:
    def __init__(self, tracks, unavailable, repeat=False):
        self.tracks = tracks
        self.repeat = repeat
        self.current = 0
        self.next_requests = 0
        self.reports = []
        self.tasks = []
        self.player, self.backend, _ = _setup(unavailable)
        self.player.queue = QobuzQueue()
        self.handler = PlaybackCommandHandler(self.player)
        self.ws = WsManager(Config())
        self.ws.on_disconnected(self.handler.note_disconnected)
        self.ws._is_connected = True
        self.ws._ws = AsyncMock()
        self.ws._ws.send.side_effect = self.receive
        self.player.set_next_track_callbacks(
            self.handler.get_next_track_info,
            self.handler.clear_next_track_info,
        )
        # Keep this harness runnable against v1.7.0-era source archives. The
        # old code must fail on playback behavior, not on a missing test hook.
        if hasattr(self.ws, "request_next_track"):
            self.player.set_next_track_request_callback(self.ws.request_next_track)
        self.player.set_state_reporter(
            StateReporter(
                self.player,
                self.player.queue,
                self.report,
            )
        )
        for msg_type in self.handler.get_message_types():
            self.ws.register_handler(msg_type, self.dispatch)

    def dispatch(self, msg_type, message):
        task = self.handler.dispatch_message(msg_type, message)
        self.tasks.append(task)
        return task

    async def report(self, report):
        await self.ws.send_state_update(
            playing_state=int(report.to_proto_dict()["playingState"]),
            buffer_state=int(report.buffer_state),
            position_ms=report.position_value_ms,
            duration_ms=report.duration_ms,
            queue_item_id=report.current_queue_item_id,
            queue_version_major=report.queue_version_major,
            queue_version_minor=report.queue_version_minor,
        )

    async def receive(self, frame):
        decoded = self.ws._codec.decode_frame(frame)
        batch = self.ws._codec.decode_qconnect_batch(decoded.payload)
        for message in batch.messages:
            if message.messageType == 23:
                state = message.rndrSrvrStateUpdated.state
                self.reports.append(state)
                if (state.queueVersion.major, state.queueVersion.minor) == (42, 7):
                    if 0 <= state.currentQueueItemId < len(self.tracks):
                        self.current = state.currentQueueItemId
                # No unsolicited response, matching the reopened issue.
            elif message.messageType == 24:
                assert message.SerializeToString() == bytes.fromhex("08 18 c2 01 02 10 02")
                self.next_requests += 1
                self.current += 1
                if self.repeat:
                    self.current %= len(self.tracks)
                self.tasks.append(asyncio.create_task(self.send_state(self.current)))

    async def send(self, message):
        batch = pb.QConnectBatch()
        batch.messages.append(message)
        await self.ws._handle_payload(
            DecodedMessage(
                msg_type=MessageType.PAYLOAD,
                payload=batch.SerializeToString(),
            )
        )

    async def send_state(self, index):
        message = pb.QConnectMessage(messageType=41)
        state = message.srvrRndrSetState
        state.queueVersion.major = 42
        state.queueVersion.minor = 7
        state.playingState = 2 if index < len(self.tracks) else 1
        if index < len(self.tracks):
            state.currentQueueItem.trackId = int(self.tracks[index])
            state.currentQueueItem.queueItemId = index
            if index + 1 < len(self.tracks):
                state.nextQueueItem.trackId = int(self.tracks[index + 1])
                state.nextQueueItem.queueItemId = index + 1
        await self.send(message)

    async def drain(self):
        for _ in range(100):
            pending = [task for task in self.tasks if not task.done()]
            if not pending:
                # Surface exceptions, including in tasks that already finished.
                await asyncio.gather(*self.tasks)
                return
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
        pytest.fail("Protocol exchange did not converge")

    async def start(self):
        message = pb.QConnectMessage(messageType=43)
        message.srvrRndrSetActive.active = True
        await self.send(message)
        await self.drain()
        await self.send_state(0)
        await self.drain()


@pytest.mark.parametrize("unavailable", [("500",), ("500", "501"), ("500", "500")])
async def test_advances_without_unsolicited_state_response(unavailable, monkeypatch):
    from qobuz_proxy.playback import player as player_module

    monkeypatch.setattr(player_module, "_UNAVAILABLE_SKIP_TIMEOUT_S", 0.05)
    session = Session(["1", *unavailable, "6"], unavailable)
    await session.start()
    await session.player._handle_track_ended(session.player.current_track)
    await session.drain()
    if session.player._skip_timeout_task is not None:
        await asyncio.wait_for(session.player._skip_timeout_task, timeout=1)
    assert session.backend.played == ["1", "6"]
    assert session.player.state == PlaybackState.PLAYING
    assert session.next_requests == len(unavailable)
    assert session.player._skip_timeout_task is None
    assert all((r.queueVersion.major, r.queueVersion.minor) == (42, 7) for r in session.reports)


async def test_final_unavailable_item_stops_on_server_response():
    session = Session(["1", "500"], ["500"])
    await session.start()
    await session.player._handle_track_ended(session.player.current_track)
    await session.drain()
    assert session.backend.played == ["1"]
    assert session.player.state == PlaybackState.STOPPED
    assert session.player._skip_timeout_task is None
    assert session.next_requests == 1


async def test_repeat_all_unavailable_is_bounded(monkeypatch):
    from qobuz_proxy.playback import player as player_module

    monkeypatch.setattr(player_module, "_MAX_UNAVAILABLE_SKIPS", 3)
    session = Session(["500", "501"], ["500", "501"], repeat=True)
    await session.start()
    assert session.backend.played == []
    assert session.player.state == PlaybackState.STOPPED
    assert session.player._skip_timeout_task is None
    assert session.next_requests == 3


async def test_echo_while_next_is_pending_does_not_cancel_or_duplicate_skip():
    session = Session(["500", "6"], ["500"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_request(still_needed):
        entered.set()
        await release.wait()
        return await session.ws.request_next_track(still_needed)

    session.player.set_next_track_request_callback(delayed_request)
    start = asyncio.create_task(session.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        await asyncio.wait_for(
            session.player.apply_remote_state(
                track_id="500",
                queue_item_id=0,
                position_ms=0,
                playing_state=2,
            ),
            timeout=1,
        )
    finally:
        release.set()
    await start
    assert session.backend.played == ["6"]
    assert session.next_requests == 1


@pytest.mark.parametrize("interruption", ["stop", "pause", "deactivate", "disconnect"])
async def test_pending_next_cannot_override_new_intent(interruption):
    session = Session(["500", "6"], ["500"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_request(still_needed):
        entered.set()
        await release.wait()
        return await session.ws.request_next_track(still_needed)

    session.player.set_next_track_request_callback(delayed_request)
    start = asyncio.create_task(session.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    command = None
    if interruption == "deactivate":
        message = pb.QConnectMessage(messageType=43)
        message.srvrRndrSetActive.active = False
        await session.send(message)
    elif interruption == "disconnect":
        session.ws._invalidate_connection()
    else:
        command = asyncio.create_task(
            session.player.apply_remote_state(
                track_id=None,
                queue_item_id=None,
                position_ms=None,
                playing_state=1 if interruption == "stop" else 3,
            )
        )
        await asyncio.sleep(0)
    release.set()
    await start
    if command:
        await command
    await session.drain()
    assert session.next_requests == 0
    assert session.backend.played == []
    assert session.player._skip_timeout_task is None


async def test_missing_queue_version_in_later_command_preserves_known_version():
    session = Session(["1", "6"], [])
    await session.start()
    message = pb.QConnectMessage(messageType=41)
    message.srvrRndrSetState.playingState = 3
    await session.send(message)
    await session.drain()
    version = await session.player.queue.get_version()
    assert (version.major, version.minor) == (42, 7)


async def test_reconnect_can_resume_an_interrupted_unavailable_skip():
    session = Session(["500", "6"], ["500"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_request(still_needed):
        entered.set()
        await release.wait()
        return await session.ws.request_next_track(still_needed)

    session.player.set_next_track_request_callback(delayed_request)
    start = asyncio.create_task(session.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    session.ws._invalidate_connection()
    release.set()
    await start
    assert session.next_requests == 0
    assert session.player._skip_timeout_task is None

    session.player.set_next_track_request_callback(session.ws.request_next_track)
    session.ws._is_connected = True
    session.handler.note_connected()
    await session.start()
    assert session.next_requests == 1
    assert session.backend.played == ["6"]


async def test_unanswered_next_times_out_once(monkeypatch):
    from qobuz_proxy.playback import player as player_module

    monkeypatch.setattr(player_module, "_UNAVAILABLE_SKIP_TIMEOUT_S", 0.05)
    session = Session(["500", "6"], ["500"])
    original = session.ws._ws.send.side_effect
    actions = []

    async def no_ack(frame):
        decoded = session.ws._codec.decode_frame(frame)
        message = session.ws._codec.decode_qconnect_batch(decoded.payload).messages[0]
        if message.messageType == 24:
            actions.append(message)
        else:
            await original(frame)

    session.ws._ws.send.side_effect = no_ack
    await session.start()
    await asyncio.wait_for(session.player._skip_timeout_task, timeout=1)
    assert len(actions) == 1
    assert session.backend.played == []
    assert session.player.state == PlaybackState.STOPPED
