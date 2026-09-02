"""Tests for how the player's internal state is reported on the wire."""

from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends import BufferStatus, PlaybackState
from qobuz_proxy.playback.state_reporter import StateReporter, wire_playing_state


def _reporter(player_state: PlaybackState):
    player = MagicMock()
    player.state = player_state
    player.current_track = None
    player.current_position_ms = 0
    player.duration_ms = 0
    player._position_timestamp_ms = 0
    player._position_value_ms = 0
    player.backend.get_buffer_status = AsyncMock(return_value=BufferStatus.OK)

    queue = MagicMock()
    queue_state = MagicMock()
    queue_state.version.major = 1
    queue_state.version.minor = 0
    queue.get_state = AsyncMock(return_value=queue_state)

    sent = []

    async def send(report):
        sent.append(report)

    return StateReporter(player=player, queue=queue, send_callback=send), sent


class TestWirePlayingState:
    def test_loading_is_playing(self) -> None:
        assert wire_playing_state(PlaybackState.LOADING) == PlaybackState.PLAYING

    def test_error_is_stopped(self) -> None:
        assert wire_playing_state(PlaybackState.ERROR) == PlaybackState.STOPPED

    def test_protocol_states_pass_through(self) -> None:
        for state in (PlaybackState.STOPPED, PlaybackState.PLAYING, PlaybackState.PAUSED):
            assert wire_playing_state(state) == state


class TestLoadingReport:
    async def test_loading_goes_out_as_playing_and_buffering(self) -> None:
        """Between tracks the renderer must not look stopped to the app: on the
        local backend the whole file downloads first, and a STOPPED report in
        that window made the app pause the freshly skipped-to track (#22)."""
        reporter, sent = _reporter(PlaybackState.LOADING)

        await reporter.report_now()

        report = sent[0]
        assert report.buffer_state == BufferStatus.LOW  # encodes as BUFFER_STATE_BUFFERING
        proto = report.to_proto_dict()
        assert proto["playingState"] == int(PlaybackState.PLAYING)
        assert proto["bufferState"] == 1

    async def test_playing_keeps_backend_buffer_status(self) -> None:
        reporter, sent = _reporter(PlaybackState.PLAYING)

        await reporter.report_now()

        assert sent[0].buffer_state == BufferStatus.OK
