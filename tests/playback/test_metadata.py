"""Tests for track metadata retrieval and caching."""

import time
from unittest.mock import AsyncMock

import pytest

from qobuz_proxy.playback.metadata import (
    AudioQuality,
    MetadataCache,
    MetadataService,
    TrackMetadata,
)


class TestAudioQuality:
    """Tests for AudioQuality class."""

    def test_quality_constants(self) -> None:
        """Test quality format constants."""
        assert AudioQuality.MP3_320 == 5
        assert AudioQuality.FLAC_CD == 6
        assert AudioQuality.FLAC_HIRES_96 == 7
        assert AudioQuality.FLAC_HIRES_192 == 27

    def test_get_name_known_quality(self) -> None:
        """Test getting name for known quality IDs."""
        assert AudioQuality.get_name(5) == "MP3 320kbps"
        assert AudioQuality.get_name(6) == "FLAC CD (16-bit/44.1kHz)"
        assert AudioQuality.get_name(7) == "FLAC Hi-Res (24-bit/96kHz)"
        assert AudioQuality.get_name(27) == "FLAC Hi-Res (24-bit/192kHz)"

    def test_get_name_unknown_quality(self) -> None:
        """Test getting name for unknown quality ID."""
        assert AudioQuality.get_name(99) == "Unknown (99)"


class TestTrackMetadata:
    """Tests for TrackMetadata class."""

    def test_default_values(self) -> None:
        """Test default field values."""
        metadata = TrackMetadata()
        assert metadata.track_id == ""
        assert metadata.title == ""
        assert metadata.artist == ""
        assert metadata.album == ""
        assert metadata.duration_ms == 0
        assert metadata.artwork_url == ""
        assert metadata.streaming_url == ""
        assert metadata.streaming_url_expires_at == 0
        assert metadata.actual_quality == 0

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        metadata = TrackMetadata(
            track_id="12345",
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            duration_ms=180000,
            artwork_url="https://example.com/art.jpg",
            actual_quality=27,
        )

        result = metadata.to_dict()

        assert result["track_id"] == "12345"
        assert result["title"] == "Test Track"
        assert result["artist"] == "Test Artist"
        assert result["album"] == "Test Album"
        assert result["duration_ms"] == 180000
        assert result["artwork_url"] == "https://example.com/art.jpg"
        assert result["quality"] == 27
        assert result["quality_name"] == "FLAC Hi-Res (24-bit/192kHz)"

    def test_duration_s_property(self) -> None:
        """Test duration_s property conversion."""
        metadata = TrackMetadata(duration_ms=180000)
        assert metadata.duration_s == 180.0

        metadata = TrackMetadata(duration_ms=0)
        assert metadata.duration_s == 0.0

    def test_is_url_expired_no_url(self) -> None:
        """Test URL expiry check with no URL."""
        metadata = TrackMetadata()
        assert metadata.is_url_expired() is True

    def test_is_url_expired_no_expiry(self) -> None:
        """Test URL expiry check with no expiry time."""
        metadata = TrackMetadata(streaming_url="https://example.com/stream")
        assert metadata.is_url_expired() is True

    def test_is_url_expired_valid(self) -> None:
        """Test URL expiry check with valid URL."""
        future_time = int(time.time()) + 300  # 5 minutes in future
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=future_time,
        )
        assert metadata.is_url_expired() is False

    def test_is_url_expired_expired(self) -> None:
        """Test URL expiry check with expired URL."""
        past_time = int(time.time()) - 10  # 10 seconds ago
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=past_time,
        )
        assert metadata.is_url_expired() is True

    def test_is_url_expired_within_buffer(self) -> None:
        """Test URL expiry check within buffer period."""
        # Expires in 20 seconds, default buffer is 30
        near_future = int(time.time()) + 20
        metadata = TrackMetadata(
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=near_future,
        )
        assert metadata.is_url_expired() is True

        # But with smaller buffer, it's not expired
        assert metadata.is_url_expired(buffer_s=10) is False


class TestMetadataCache:
    """Tests for MetadataCache class."""

    def test_get_empty_cache(self) -> None:
        """Test getting from empty cache returns None."""
        cache = MetadataCache()
        assert cache.get("12345") is None

    def test_set_and_get(self) -> None:
        """Test setting and getting cache entry."""
        cache = MetadataCache()
        metadata = TrackMetadata(track_id="12345", title="Test")

        cache.set("12345", metadata)

        result = cache.get("12345")
        assert result is not None
        assert result.track_id == "12345"
        assert result.title == "Test"

    def test_clear(self) -> None:
        """Test clearing cache."""
        cache = MetadataCache()
        cache.set("12345", TrackMetadata(track_id="12345"))
        cache.set("67890", TrackMetadata(track_id="67890"))

        cache.clear()

        assert cache.get("12345") is None
        assert cache.get("67890") is None

    def test_invalidate_url(self) -> None:
        """Test invalidating URL keeps metadata."""
        cache = MetadataCache()
        metadata = TrackMetadata(
            track_id="12345",
            title="Test",
            streaming_url="https://example.com/stream",
            streaming_url_expires_at=int(time.time()) + 300,
        )
        cache.set("12345", metadata)

        cache.invalidate_url("12345")

        result = cache.get("12345")
        assert result is not None
        assert result.title == "Test"  # Metadata preserved
        assert result.streaming_url == ""  # URL cleared
        assert result.streaming_url_expires_at == 0

    def test_invalidate_url_nonexistent(self) -> None:
        """Test invalidating URL for nonexistent track does nothing."""
        cache = MetadataCache()
        cache.invalidate_url("nonexistent")  # Should not raise

    def test_lru_eviction(self) -> None:
        """Test LRU eviction when cache is full."""
        cache = MetadataCache()
        cache._max_size = 3  # Small cache for testing

        # Add 3 entries
        cache.set("1", TrackMetadata(track_id="1"))
        cache.set("2", TrackMetadata(track_id="2"))
        cache.set("3", TrackMetadata(track_id="3"))

        # All should be present
        assert cache.get("1") is not None
        assert cache.get("2") is not None
        assert cache.get("3") is not None

        # Add 4th entry, should evict oldest ("1")
        cache.set("4", TrackMetadata(track_id="4"))

        assert cache.get("1") is None  # Evicted
        assert cache.get("2") is not None
        assert cache.get("3") is not None
        assert cache.get("4") is not None

    def test_update_existing_no_eviction(self) -> None:
        """Test updating existing entry doesn't trigger eviction."""
        cache = MetadataCache()
        cache._max_size = 2

        cache.set("1", TrackMetadata(track_id="1", title="Original"))
        cache.set("2", TrackMetadata(track_id="2"))

        # Update "1" shouldn't evict "2"
        cache.set("1", TrackMetadata(track_id="1", title="Updated"))

        assert cache.get("1") is not None
        assert cache.get("1").title == "Updated"
        assert cache.get("2") is not None


class MockAPIClient:
    """Mock API client for testing MetadataService."""

    def __init__(self) -> None:
        self.get_track_metadata = AsyncMock()
        self.get_track_url = AsyncMock()


@pytest.fixture
def mock_api() -> MockAPIClient:
    """Create a mock API client."""
    return MockAPIClient()


@pytest.fixture
def metadata_service(mock_api: MockAPIClient) -> MetadataService:
    """Create a MetadataService with mock API client."""
    return MetadataService(mock_api, max_quality=27)  # type: ignore[arg-type]


class TestMetadataService:
    """Tests for MetadataService class."""

    @pytest.mark.asyncio
    async def test_get_metadata_success(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test successful metadata retrieval."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "https://example.com/art.jpg",
        }

        result = await metadata_service.get_metadata("12345")

        assert result is not None
        assert result.track_id == "12345"
        assert result.title == "Test Track"
        assert result.artist == "Test Artist"
        assert result.album == "Test Album"
        assert result.duration_ms == 180000
        assert result.artwork_url == "https://example.com/art.jpg"

    @pytest.mark.asyncio
    async def test_get_metadata_not_found(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata retrieval for nonexistent track."""
        mock_api.get_track_metadata.return_value = None

        result = await metadata_service.get_metadata("99999999")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_metadata_cache_hit(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata is cached and reused."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }

        # First call
        result1 = await metadata_service.get_metadata("12345")
        # Second call
        result2 = await metadata_service.get_metadata("12345")

        assert result1 is not None
        assert result2 is not None
        assert result1 is result2  # Same object
        # API called only once
        assert mock_api.get_track_metadata.call_count == 1

    @pytest.mark.asyncio
    async def test_get_metadata_with_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test metadata retrieval with streaming URL."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 192000,
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.streaming_url == "https://streaming.example.com/track.flac"
        assert result.actual_quality == 27

    @pytest.mark.asyncio
    async def test_get_streaming_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test streaming URL retrieval."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.return_value = {
            "url": "https://streaming.example.com/track.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 192000,
            "mime_type": "audio/flac",
        }

        result = await metadata_service.get_streaming_url("12345")

        assert result == "https://streaming.example.com/track.flac"

    @pytest.mark.asyncio
    async def test_quality_fallback(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test quality fallback when preferred quality unavailable."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        # Return None for quality 27 and 7, dict for quality 6
        mock_api.get_track_url.side_effect = [
            None,  # 27 fails
            None,  # 7 fails
            {  # 6 succeeds
                "url": "https://streaming.example.com/track.flac",
                "format_id": 6,
                "bit_depth": 16,
                "sampling_rate": 44100,
                "mime_type": "audio/flac",
            },
        ]

        result = await metadata_service.get_metadata("12345", fetch_url=True)

        assert result is not None
        assert result.streaming_url == "https://streaming.example.com/track.flac"
        assert result.actual_quality == 6  # Fell back to CD quality

    @pytest.mark.asyncio
    async def test_refresh_streaming_url(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test forced URL refresh."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }
        mock_api.get_track_url.side_effect = [
            {
                "url": "https://streaming.example.com/first.flac",
                "format_id": 27,
                "bit_depth": 24,
                "sampling_rate": 192000,
                "mime_type": "audio/flac",
            },
            {
                "url": "https://streaming.example.com/second.flac",
                "format_id": 27,
                "bit_depth": 24,
                "sampling_rate": 192000,
                "mime_type": "audio/flac",
            },
        ]

        # Get initial URL
        url1 = await metadata_service.get_streaming_url("12345")
        # Force refresh
        url2 = await metadata_service.refresh_streaming_url("12345")

        assert url1 == "https://streaming.example.com/first.flac"
        assert url2 == "https://streaming.example.com/second.flac"

    @pytest.mark.asyncio
    async def test_preload_tracks(
        self, metadata_service: MetadataService, mock_api: MockAPIClient
    ) -> None:
        """Test preloading multiple tracks."""
        mock_api.get_track_metadata.return_value = {
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration_ms": 180000,
            "album_art_url": "",
        }

        await metadata_service.preload_tracks(["1", "2", "3"])

        # All tracks should be fetched
        assert mock_api.get_track_metadata.call_count == 3

        # Subsequent gets should be cached
        mock_api.get_track_metadata.reset_mock()
        await metadata_service.get_metadata("1")
        await metadata_service.get_metadata("2")
        await metadata_service.get_metadata("3")

        # No new API calls
        assert mock_api.get_track_metadata.call_count == 0

    def test_get_quality_fallback_order(self, metadata_service: MetadataService) -> None:
        """Test quality fallback order generation."""
        # Default max_quality is 27
        assert metadata_service._get_quality_fallback_order() == [27, 7, 6, 5]

        # Test with lower max_quality
        service = MetadataService(MockAPIClient(), max_quality=7)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [7, 6, 5]

        service = MetadataService(MockAPIClient(), max_quality=6)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [6, 5]

        service = MetadataService(MockAPIClient(), max_quality=5)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [5]

    def test_get_quality_fallback_order_invalid(self, metadata_service: MetadataService) -> None:
        """Test quality fallback with invalid max_quality returns full list."""
        service = MetadataService(MockAPIClient(), max_quality=99)  # type: ignore[arg-type]
        assert service._get_quality_fallback_order() == [27, 7, 6, 5]

    def test_log_now_playing(
        self, metadata_service: MetadataService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test now playing log output."""
        import logging

        metadata = TrackMetadata(
            track_id="12345",
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            actual_quality=27,
        )

        with caplog.at_level(logging.INFO):
            metadata_service.log_now_playing(metadata)

        assert "Now playing: Test Artist - Test Track" in caplog.text
        assert "[Test Album]" in caplog.text
        assert "FLAC Hi-Res (24-bit/192kHz)" in caplog.text
