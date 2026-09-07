"""Tests for DLNABackend._build_didl metadata generation."""

from qobuz_proxy.backends.types import BackendTrackMetadata
from qobuz_proxy.backends.dlna.backend import DLNABackend


def _make_backend() -> DLNABackend:
    backend = DLNABackend.__new__(DLNABackend)
    backend._capabilities = None
    return backend


def _make_metadata(**kwargs) -> BackendTrackMetadata:  # type: ignore[no-untyped-def]
    defaults = {
        "track_id": "1",
        "title": "Test Track",
        "artist": "Test Artist",
        "album": "Test Album",
        "duration_ms": 180000,
        "artwork_url": "",
    }
    defaults.update(kwargs)
    return BackendTrackMetadata(**defaults)


class TestBuildDidl:
    def test_hires_96k_includes_correct_audio_attributes(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=96000, bit_depth=24)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        assert 'sampleFrequency="96000"' in didl
        assert 'bitsPerSample="24"' in didl

    def test_hires_192k_includes_correct_audio_attributes(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=192000, bit_depth=24)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        assert 'sampleFrequency="192000"' in didl
        assert 'bitsPerSample="24"' in didl

    def test_cd_quality_includes_correct_audio_attributes(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=44100, bit_depth=16)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        assert 'sampleFrequency="44100"' in didl
        assert 'bitsPerSample="16"' in didl

    def test_no_audio_attributes_when_format_unknown(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=0, bit_depth=0)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        assert "sampleFrequency" not in didl
        assert "bitsPerSample" not in didl

    def test_audio_attributes_are_on_res_element(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=96000, bit_depth=24)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        # Attributes must appear inside the <res ...> tag, not elsewhere
        res_start = didl.index("<res ")
        res_end = didl.index(">", res_start)
        res_tag = didl[res_start:res_end]
        assert 'sampleFrequency="96000"' in res_tag
        assert 'bitsPerSample="24"' in res_tag

    def test_track_metadata_in_didl(self) -> None:
        backend = _make_backend()
        metadata = _make_metadata(sample_rate=96000, bit_depth=24)

        didl = backend._build_didl("http://proxy/track.flac", metadata)

        assert "Test Track" in didl
        assert "Test Artist" in didl
        assert "Test Album" in didl


class TestQualityDetectionConfirmed:
    """Backend exposes whether auto-detected quality came from explicit device info."""

    def test_unconfirmed_without_capabilities(self):
        from qobuz_proxy.backends.dlna.backend import DLNABackend

        backend = DLNABackend("192.168.1.10")
        assert backend.quality_detection_confirmed is False

    def test_follows_capabilities_flag(self):
        from qobuz_proxy.backends.dlna.backend import DLNABackend
        from qobuz_proxy.backends.dlna.capabilities import parse_protocol_info_sink

        backend = DLNABackend("192.168.1.10")

        backend._capabilities = parse_protocol_info_sink(
            "http-get:*:audio/flac:DLNA.ORG_PN=FLAC_192;DLNA.ORG_OP=01"
        )
        assert backend.quality_detection_confirmed is True

        backend._capabilities = parse_protocol_info_sink("http-get:*:audio/x-flac:*")
        assert backend.quality_detection_confirmed is False


class TestSonosGaplessQueue:
    """Sonos gapless arming appends to the device queue — duplicates replay the song."""

    def _make_sonos_backend(self):
        from unittest.mock import AsyncMock, MagicMock

        backend = DLNABackend("10.0.0.5")
        backend._is_sonos = True
        backend._gapless_supported = True
        client = MagicMock()
        client.add_uri_to_queue = AsyncMock(return_value=7)
        client.remove_track_from_queue = AsyncMock(return_value=True)
        backend._current_proxy_url = "http://proxy/audio/current.flac"
        client.get_track_uri = AsyncMock(return_value=backend._current_proxy_url)
        backend._client = client
        return backend, client

    async def test_set_next_track_stores_queue_position(self):
        backend, client = self._make_sonos_backend()
        meta = _make_metadata(track_id="222")

        assert await backend.set_next_track("http://proxy/audio/222_9.flac", meta, 9)

        client.add_uri_to_queue.assert_awaited_once()
        assert backend._next_track_queue_nr == 7

    async def test_set_next_track_skips_duplicate_url(self):
        backend, client = self._make_sonos_backend()
        meta = _make_metadata(track_id="222")

        assert await backend.set_next_track("http://proxy/audio/222_9.flac", meta, 9)
        assert await backend.set_next_track("http://proxy/audio/222_9.flac", meta, 9)

        client.add_uri_to_queue.assert_awaited_once()

    async def test_clear_next_track_removes_queued_entry(self):
        backend, client = self._make_sonos_backend()
        meta = _make_metadata(track_id="222")
        await backend.set_next_track("http://proxy/audio/222_9.flac", meta, 9)

        await backend.clear_next_track()

        client.remove_track_from_queue.assert_awaited_once_with(7)
        assert backend._next_track_queue_nr is None
        assert backend._next_track_proxy_url is None

    async def test_clear_next_track_without_armed_entry_is_noop(self):
        backend, client = self._make_sonos_backend()

        await backend.clear_next_track()

        client.remove_track_from_queue.assert_not_called()
