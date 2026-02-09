"""Tests for audio backend interface and factory."""

import pytest

from qobuz_proxy.backends import (
    AudioBackend,
    BackendFactory,
    BackendInfo,
    BackendNotFoundError,
    BackendRegistry,
    BackendTrackMetadata,
    BufferStatus,
    PlaybackState,
)
from qobuz_proxy.config import BackendConfig, Config


class TestPlaybackState:
    """Tests for PlaybackState enum."""

    def test_values_match_protocol(self) -> None:
        """Test enum values match Qobuz Connect protocol."""
        assert PlaybackState.STOPPED == 1
        assert PlaybackState.PLAYING == 2
        assert PlaybackState.PAUSED == 3
        assert PlaybackState.LOADING == 4
        assert PlaybackState.ERROR == 5

    def test_all_states_defined(self) -> None:
        """Test all expected states are defined."""
        states = [s.name for s in PlaybackState]
        assert "STOPPED" in states
        assert "PLAYING" in states
        assert "PAUSED" in states
        assert "LOADING" in states
        assert "ERROR" in states


class TestBufferStatus:
    """Tests for BufferStatus enum."""

    def test_values(self) -> None:
        """Test buffer status values."""
        assert BufferStatus.EMPTY == 0
        assert BufferStatus.LOW == 1
        assert BufferStatus.OK == 2
        assert BufferStatus.FULL == 3

    def test_all_statuses_defined(self) -> None:
        """Test all expected statuses are defined."""
        statuses = [s.name for s in BufferStatus]
        assert "EMPTY" in statuses
        assert "LOW" in statuses
        assert "OK" in statuses
        assert "FULL" in statuses


class TestBackendTrackMetadata:
    """Tests for BackendTrackMetadata dataclass."""

    def test_defaults(self) -> None:
        """Test default values."""
        metadata = BackendTrackMetadata(track_id="12345")
        assert metadata.track_id == "12345"
        assert metadata.title == ""
        assert metadata.artist == ""
        assert metadata.album == ""
        assert metadata.duration_ms == 0
        assert metadata.artwork_url == ""

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        metadata = BackendTrackMetadata(
            track_id="12345",
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            duration_ms=180000,
            artwork_url="https://example.com/art.jpg",
        )

        result = metadata.to_dict()

        assert result["track_id"] == "12345"
        assert result["title"] == "Test Track"
        assert result["artist"] == "Test Artist"
        assert result["album"] == "Test Album"
        assert result["duration_ms"] == 180000
        assert result["artwork_url"] == "https://example.com/art.jpg"


class TestBackendInfo:
    """Tests for BackendInfo dataclass."""

    def test_str_with_ip(self) -> None:
        """Test string representation with IP."""
        info = BackendInfo(
            backend_type="dlna",
            name="Living Room Speaker",
            device_id="uuid-123",
            ip="192.168.1.100",
            port=1400,
        )
        assert str(info) == "Living Room Speaker (dlna) @ 192.168.1.100:1400"

    def test_str_without_ip(self) -> None:
        """Test string representation without IP."""
        info = BackendInfo(
            backend_type="dlna",
            name="Test Backend",
            device_id="test-id",
        )
        assert str(info) == "Test Backend (dlna)"


class TestBackendRegistry:
    """Tests for BackendRegistry."""

    def test_dlna_registered(self) -> None:
        """Test DLNA backend is registered."""
        assert "dlna" in BackendRegistry.available_types()

    def test_get_unregistered(self) -> None:
        """Test getting unregistered backend returns None."""
        backend_class = BackendRegistry.get("nonexistent")
        assert backend_class is None


class TestBackendFactory:
    """Tests for BackendFactory."""

    @pytest.mark.asyncio
    async def test_create_unknown_type_raises(self) -> None:
        """Test creating unknown backend type raises error."""
        config = Config()
        config.backend = BackendConfig(type="unknown_type")

        with pytest.raises(BackendNotFoundError) as exc_info:
            await BackendFactory.create_from_config(config)

        assert "unknown_type" in str(exc_info.value)
        assert "Available types:" in str(exc_info.value)

    def test_list_available_backends(self) -> None:
        """Test listing available backends."""
        available = BackendFactory.list_available_backends()
        assert "dlna" in available

    @pytest.mark.asyncio
    async def test_create_dlna_connection_failure(self) -> None:
        """Test DLNA backend creation fails gracefully when device unavailable."""
        with pytest.raises(BackendNotFoundError):
            await BackendFactory.create_dlna(ip="192.168.1.100")


class TestAudioBackendInterface:
    """Tests verifying AudioBackend interface completeness."""

    def test_interface_has_play(self) -> None:
        """Test interface defines play method."""
        assert hasattr(AudioBackend, "play")
        assert callable(getattr(AudioBackend, "play"))

    def test_interface_has_pause(self) -> None:
        """Test interface defines pause method."""
        assert hasattr(AudioBackend, "pause")
        assert callable(getattr(AudioBackend, "pause"))

    def test_interface_has_resume(self) -> None:
        """Test interface defines resume method."""
        assert hasattr(AudioBackend, "resume")
        assert callable(getattr(AudioBackend, "resume"))

    def test_interface_has_stop(self) -> None:
        """Test interface defines stop method."""
        assert hasattr(AudioBackend, "stop")
        assert callable(getattr(AudioBackend, "stop"))

    def test_interface_has_seek(self) -> None:
        """Test interface defines seek method."""
        assert hasattr(AudioBackend, "seek")
        assert callable(getattr(AudioBackend, "seek"))

    def test_interface_has_get_position(self) -> None:
        """Test interface defines get_position method."""
        assert hasattr(AudioBackend, "get_position")
        assert callable(getattr(AudioBackend, "get_position"))

    def test_interface_has_set_volume(self) -> None:
        """Test interface defines set_volume method."""
        assert hasattr(AudioBackend, "set_volume")
        assert callable(getattr(AudioBackend, "set_volume"))

    def test_interface_has_get_volume(self) -> None:
        """Test interface defines get_volume method."""
        assert hasattr(AudioBackend, "get_volume")
        assert callable(getattr(AudioBackend, "get_volume"))

    def test_interface_has_get_state(self) -> None:
        """Test interface defines get_state method."""
        assert hasattr(AudioBackend, "get_state")
        assert callable(getattr(AudioBackend, "get_state"))

    def test_interface_has_connect(self) -> None:
        """Test interface defines connect method."""
        assert hasattr(AudioBackend, "connect")
        assert callable(getattr(AudioBackend, "connect"))

    def test_interface_has_disconnect(self) -> None:
        """Test interface defines disconnect method."""
        assert hasattr(AudioBackend, "disconnect")
        assert callable(getattr(AudioBackend, "disconnect"))
