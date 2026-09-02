"""
Track metadata retrieval and caching.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from qobuz_proxy.auth.api_client import QobuzAPIClient
    from qobuz_proxy.backends import BackendTrackMetadata

logger = logging.getLogger(__name__)


class AudioQuality:
    """Qobuz audio quality format IDs."""

    MP3_320 = 5  # MP3 320 kbps
    FLAC_CD = 6  # FLAC 16-bit/44.1kHz
    FLAC_HIRES_96 = 7  # FLAC 24-bit/96kHz
    FLAC_HIRES_192 = 27  # FLAC 24-bit/192kHz

    NAMES: dict[int, str] = {
        5: "MP3 320kbps",
        6: "FLAC CD (16-bit/44.1kHz)",
        7: "FLAC Hi-Res (24-bit/96kHz)",
        27: "FLAC Hi-Res (24-bit/192kHz)",
    }

    @classmethod
    def get_name(cls, quality_id: int) -> str:
        """Get human-readable name for quality ID."""
        return cls.NAMES.get(quality_id, f"Unknown ({quality_id})")


@dataclass
class TrackMetadata:
    """Track metadata."""

    track_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    artwork_url: str = ""

    # Streaming info (fetched separately)
    streaming_url: str = ""
    streaming_url_expires_at: int = 0  # Timestamp in seconds
    actual_quality: int = 0  # Quality ID of the streaming URL
    sample_rate: int = 0  # Hz, e.g. 44100, 96000, 192000
    bit_depth: int = 0  # e.g. 16, 24
    blob: str = ""  # Opaque token from getFileUrl, needed for reportStreamingEnd

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "artwork_url": self.artwork_url,
            "quality": self.actual_quality,
            "quality_name": AudioQuality.get_name(self.actual_quality),
        }

    def is_url_expired(self, buffer_s: int = 30) -> bool:
        """Check if streaming URL is expired or will expire soon."""
        if not self.streaming_url or not self.streaming_url_expires_at:
            return True
        now_s = int(time.time())
        return now_s + buffer_s >= self.streaming_url_expires_at

    @property
    def duration_s(self) -> float:
        """Duration in seconds."""
        return self.duration_ms / 1000.0


@dataclass
class MetadataCache:
    """In-memory cache for track metadata."""

    _cache: dict[str, TrackMetadata] = field(default_factory=dict)
    _max_size: int = 100

    def get(self, track_id: str) -> Optional[TrackMetadata]:
        """Get cached metadata for track."""
        return self._cache.get(track_id)

    def set(self, track_id: str, metadata: TrackMetadata) -> None:
        """Cache metadata for track."""
        # Simple LRU: remove oldest if at capacity
        if len(self._cache) >= self._max_size and track_id not in self._cache:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[track_id] = metadata

    def clear(self) -> None:
        """Clear all cached metadata."""
        self._cache.clear()

    def invalidate_url(self, track_id: str) -> None:
        """Invalidate streaming URL for a track (keep metadata)."""
        if track_id in self._cache:
            self._cache[track_id].streaming_url = ""
            self._cache[track_id].streaming_url_expires_at = 0


class MetadataService:
    """
    Service for retrieving and caching track metadata.

    Uses QobuzAPIClient for API calls and maintains an in-memory cache.
    """

    # URL TTL estimate (Qobuz URLs expire after ~5 minutes)
    URL_TTL_SECONDS = 5 * 60

    # How long a track that could not be resolved (track/get 404 because it is
    # "Not available" in the user's region, or no streaming URL at any quality)
    # is remembered as unavailable. The player re-tries the upcoming track every
    # poll (~0.5s) while arming gapless, so without this a single unavailable
    # track hammers the Qobuz API twice a second for the whole preceding track.
    # Kept short so a transient API/network failure is retried soon.
    UNAVAILABLE_RETRY_SECONDS = 30.0

    def __init__(self, api_client: "QobuzAPIClient", max_quality: int = 27):
        """
        Initialize metadata service.

        Args:
            api_client: Authenticated Qobuz API client
            max_quality: Maximum audio quality to request (5, 6, 7, or 27)
        """
        self._api = api_client
        self._max_quality = max_quality
        self._cache = MetadataCache()
        # track_id -> monotonic time until which the track is considered unavailable
        self._unavailable_until: dict[str, float] = {}

    def _mark_unavailable(self, track_id: str) -> None:
        now = time.monotonic()
        # Drop expired entries so the map cannot grow with every failed track.
        self._unavailable_until = {
            tid: until for tid, until in self._unavailable_until.items() if until > now
        }
        self._unavailable_until[track_id] = now + self.UNAVAILABLE_RETRY_SECONDS

    def is_unavailable(self, track_id: str) -> bool:
        """Whether a recent lookup found this track unplayable (no metadata or no URL)."""
        until = self._unavailable_until.get(track_id)
        if until is None:
            return False
        if until <= time.monotonic():
            del self._unavailable_until[track_id]
            return False
        return True

    @property
    def max_quality(self) -> int:
        """Get current max quality setting."""
        return self._max_quality

    def set_max_quality(self, quality: int) -> None:
        """
        Update max quality and invalidate cached streaming URLs.

        Args:
            quality: New max quality ID (5, 6, 7, or 27)
        """
        if quality != self._max_quality:
            logger.info(
                f"Quality changed: {AudioQuality.get_name(self._max_quality)} -> "
                f"{AudioQuality.get_name(quality)}"
            )
            self._max_quality = quality
            # Invalidate all cached streaming URLs (keep metadata)
            for track_id in list(self._cache._cache.keys()):
                self._cache.invalidate_url(track_id)

    async def get_metadata(self, track_id: str, fetch_url: bool = False) -> Optional[TrackMetadata]:
        """
        Get track metadata, using cache when available.

        Args:
            track_id: Qobuz track ID
            fetch_url: Also fetch streaming URL

        Returns:
            TrackMetadata or None if not found
        """
        # Check cache
        cached = self._cache.get(track_id)
        if self.is_unavailable(track_id):
            # Recently failed to resolve; don't hit the API again yet.
            if cached and not fetch_url:
                return cached
            logger.debug(f"Track {track_id} recently unavailable; not retrying yet")
            return None
        if cached:
            # If URL requested and cached URL valid, return cached
            if not fetch_url or not cached.is_url_expired():
                return cached
            # Otherwise fall through to refresh URL
            metadata: TrackMetadata = cached
        else:
            # Fetch from API
            fetched = await self._fetch_metadata(track_id)
            if not fetched:
                return None
            metadata = fetched

        # Fetch URL if requested
        if fetch_url:
            await self._fetch_streaming_url(metadata)

        # Cache and return
        self._cache.set(track_id, metadata)
        return metadata

    async def get_streaming_url(self, track_id: str) -> Optional[str]:
        """
        Get streaming URL for track, refreshing if expired.

        Args:
            track_id: Qobuz track ID

        Returns:
            Streaming URL or None
        """
        metadata = await self.get_metadata(track_id, fetch_url=True)
        return (metadata.streaming_url or None) if metadata else None

    def get_track_format(self, track_id: str) -> tuple[int, int, int]:
        """Return (actual_quality, sample_rate_hz, bit_depth) for a cached track, or (0, 0, 0)."""
        cached = self._cache.get(track_id)
        if cached:
            return (cached.actual_quality, cached.sample_rate, cached.bit_depth)
        return (0, 0, 0)

    def get_track_blob(self, track_id: str) -> Optional[str]:
        """Return the cached getFileUrl blob for a track, or None if uncached."""
        cached = self._cache.get(track_id)
        if cached is None:
            return None
        return cached.blob

    def get_track_actual_quality(self, track_id: str) -> Optional[int]:
        """
        Get the actual streaming quality for a cached track.

        Args:
            track_id: Qobuz track ID

        Returns:
            Quality ID or None if track not in cache
        """
        cached = self._cache.get(track_id)
        if cached and cached.actual_quality:
            return cached.actual_quality
        return None

    async def refresh_streaming_url(self, track_id: str) -> Optional[str]:
        """
        Force refresh streaming URL for track.

        Args:
            track_id: Qobuz track ID

        Returns:
            New streaming URL or None
        """
        # Invalidate cached URL
        self._cache.invalidate_url(track_id)

        # Fetch fresh
        return await self.get_streaming_url(track_id)

    async def preload_tracks(self, track_ids: list[str]) -> None:
        """
        Preload metadata for multiple tracks.

        Args:
            track_ids: List of track IDs to preload
        """
        for track_id in track_ids:
            if not self._cache.get(track_id):
                await self.get_metadata(track_id, fetch_url=False)

    async def _fetch_metadata(self, track_id: str) -> Optional[TrackMetadata]:
        """Fetch metadata from Qobuz API."""
        try:
            data = await self._api.get_track_metadata(track_id)
            if not data:
                # Qobuz answers track/get with 404 for tracks that are "Not
                # available" in the user's region.
                logger.warning(f"No metadata found for track {track_id}")
                self._mark_unavailable(track_id)
                return None

            metadata = TrackMetadata(
                track_id=track_id,
                title=data.get("title", "Unknown"),
                artist=data.get("artist", "Unknown"),
                album=data.get("album", "Unknown"),
                duration_ms=data.get("duration_ms", 0),
                artwork_url=data.get("album_art_url", ""),
            )

            logger.debug(f"Fetched metadata: {metadata.artist} - {metadata.title}")
            return metadata

        except Exception as e:
            logger.error(f"Failed to fetch metadata for {track_id}: {e}")
            self._mark_unavailable(track_id)
            return None

    async def _fetch_streaming_url(self, metadata: TrackMetadata) -> None:
        """Fetch streaming URL for track."""
        try:
            # Try preferred quality, fall back to lower qualities
            qualities = self._get_quality_fallback_order()

            for quality in qualities:
                result = await self._api.get_track_url(metadata.track_id, quality)
                if result:
                    metadata.streaming_url = result["url"]
                    metadata.streaming_url_expires_at = int(time.time()) + self.URL_TTL_SECONDS
                    # Use the actual format_id from API response (may differ from requested)
                    actual_quality = result.get("format_id", quality)
                    metadata.actual_quality = actual_quality
                    # sampling_rate from Qobuz API is in kHz (e.g. 44.1, 96.0, 192.0)
                    sr = result.get("sampling_rate", 0)
                    metadata.sample_rate = int(float(sr) * 1000) if sr else 0
                    metadata.bit_depth = int(result.get("bit_depth", 0))
                    metadata.blob = result.get("blob", "") or ""
                    self._unavailable_until.pop(metadata.track_id, None)

                    if actual_quality != self._max_quality:
                        logger.info(
                            f"Track {metadata.track_id}: actual quality "
                            f"{AudioQuality.get_name(actual_quality)} "
                            f"(requested {AudioQuality.get_name(self._max_quality)})"
                        )
                    return

            logger.error(f"No streaming URL available for {metadata.track_id}")
            self._clear_streaming_url(metadata)
            self._mark_unavailable(metadata.track_id)

        except Exception as e:
            logger.error(f"Failed to fetch URL for {metadata.track_id}: {e}")
            self._clear_streaming_url(metadata)
            self._mark_unavailable(metadata.track_id)

    @staticmethod
    def _clear_streaming_url(metadata: TrackMetadata) -> None:
        """Drop a stale URL after a failed refresh.

        Keeping the old URL around would make `get_streaming_url` hand an
        expired URL to callers as if it were valid; failing loudly lets them
        retry instead of hitting a CDN 401/403.
        """
        metadata.streaming_url = ""
        metadata.streaming_url_expires_at = 0

    def _get_quality_fallback_order(self) -> list[int]:
        """Get quality IDs in fallback order from max_quality."""
        all_qualities = [27, 7, 6, 5]  # Highest to lowest
        try:
            start_idx = all_qualities.index(self._max_quality)
            return all_qualities[start_idx:]
        except ValueError:
            return all_qualities

    def log_now_playing(self, metadata: TrackMetadata) -> None:
        """
        Log currently playing track at INFO level.

        Args:
            metadata: Track metadata to log
        """
        quality_name = AudioQuality.get_name(metadata.actual_quality)
        logger.info(
            f"Now playing: {metadata.artist} - {metadata.title} [{metadata.album}] ({quality_name})"
        )

    def log_now_playing_info(
        self, metadata: "BackendTrackMetadata", actual_quality: Optional[int] = None
    ) -> None:
        """
        Log currently playing track at INFO level using backend metadata.

        Args:
            metadata: Backend track metadata to log
            actual_quality: Actual quality ID from API (uses max_quality if not provided)
        """
        quality_name = AudioQuality.get_name(
            actual_quality if actual_quality is not None else self._max_quality
        )
        logger.info(
            f"Now playing: {metadata.artist} - {metadata.title} [{metadata.album}] ({quality_name})"
        )
