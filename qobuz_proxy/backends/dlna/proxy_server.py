"""
Audio Proxy Server.

HTTP server that proxies audio streams from Qobuz CDN to DLNA devices,
handling URL expiration transparently.
"""

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiohttp import web, ClientSession, ClientTimeout

from .url_provider import StreamingURLProvider

logger = logging.getLogger(__name__)

# URL refresh settings
DEFAULT_URL_MAX_AGE_SECONDS = 240  # Refresh before 5-minute TTL
STREAM_CHUNK_SIZE = 64 * 1024  # 64KB chunks
REQUEST_TIMEOUT_SECONDS = 30


@dataclass
class RegisteredTrack:
    """A track registered with the proxy server."""

    track_id: str
    qobuz_url: str
    content_type: str
    url_fetched_at: float = field(default_factory=time.time)

    def is_url_expired(self, max_age: float = DEFAULT_URL_MAX_AGE_SECONDS) -> bool:
        """Check if the URL has expired or is about to expire."""
        age = time.time() - self.url_fetched_at
        return age >= max_age


class AudioProxyServer:
    """
    Local HTTP proxy server for DLNA audio streaming.

    Provides stable local URLs to DLNA devices while handling:
    - Qobuz URL expiration (5-minute TTL)
    - HTTP range requests for seeking
    - Streaming without full buffering

    Usage:
        url_provider = MetadataServiceURLProvider(metadata_service)
        proxy = AudioProxyServer(
            url_provider=url_provider,
            host="0.0.0.0",
            port=7120,
        )
        await proxy.start()

        # Register a track before playback
        proxy_url = proxy.register_track("12345", qobuz_url, "audio/flac")
        # proxy_url = "http://192.168.1.100:7120/audio/12345.flac"

        # Pass proxy_url to DLNA device
    """

    def __init__(
        self,
        url_provider: StreamingURLProvider,
        host: str = "0.0.0.0",
        port: int = 7120,
        url_max_age: float = DEFAULT_URL_MAX_AGE_SECONDS,
    ):
        """
        Initialize audio proxy server.

        Args:
            url_provider: Provider for fetching fresh streaming URLs
            host: Host to bind to
            port: Port to listen on
            url_max_age: Maximum URL age before refresh (seconds)
        """
        self._url_provider = url_provider
        self._host = host
        self._port = port
        self._url_max_age = url_max_age

        self._tracks: Dict[str, RegisteredTrack] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Will be set after start() to actual bound address
        self._actual_host: Optional[str] = None

    @property
    def base_url(self) -> str:
        """Get the base URL for this proxy server."""
        host = self._actual_host or self._host
        # Use actual IP if bound to 0.0.0.0
        if host == "0.0.0.0":
            host = self._get_local_ip()
        return f"http://{host}:{self._port}"

    @property
    def is_running(self) -> bool:
        """Check if the server is running."""
        return self._site is not None

    async def start(self) -> None:
        """Start the proxy server."""
        self._app = web.Application()
        self._app.router.add_get("/audio/{track_id}", self._handle_audio)
        self._app.router.add_get("/audio/{track_id}.flac", self._handle_audio)
        self._app.router.add_get("/audio/{track_id}.mp3", self._handle_audio)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        logger.info(f"Audio proxy server started on {self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the proxy server."""
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        self._tracks.clear()
        logger.info("Audio proxy server stopped")

    def register_track(
        self,
        track_id: str,
        qobuz_url: str,
        content_type: str = "audio/flac",
    ) -> str:
        """
        Register a track for proxying.

        Args:
            track_id: Qobuz track ID
            qobuz_url: Current Qobuz streaming URL
            content_type: MIME type of the audio

        Returns:
            Local proxy URL for the track
        """
        self._tracks[track_id] = RegisteredTrack(
            track_id=track_id,
            qobuz_url=qobuz_url,
            content_type=content_type,
            url_fetched_at=time.time(),
        )

        # Determine extension from content type
        ext = "flac" if "flac" in content_type else "mp3"
        proxy_url = f"{self.base_url}/audio/{track_id}.{ext}"

        logger.debug(f"Registered track {track_id} -> {proxy_url}")
        return proxy_url

    def unregister_track(self, track_id: str) -> None:
        """Remove a track from the registry."""
        if track_id in self._tracks:
            del self._tracks[track_id]
            logger.debug(f"Unregistered track {track_id}")

    def update_track_url(self, track_id: str, qobuz_url: str) -> None:
        """Update the Qobuz URL for a registered track."""
        if track_id in self._tracks:
            track = self._tracks[track_id]
            track.qobuz_url = qobuz_url
            track.url_fetched_at = time.time()
            logger.debug(f"Updated URL for track {track_id}")

    async def _handle_audio(self, request: web.Request) -> web.StreamResponse:
        """Handle audio stream requests from DLNA devices."""
        # Extract track ID (remove extension if present)
        track_id = request.match_info["track_id"]
        track_id = track_id.rsplit(".", 1)[0]  # Remove .flac/.mp3

        # Check if track is registered
        track = self._tracks.get(track_id)
        if not track:
            logger.warning(f"Unknown track requested: {track_id}")
            return web.Response(status=404, text="Track not found")

        # Check if URL needs refresh
        if track.is_url_expired(self._url_max_age):
            logger.info(f"Refreshing expired URL for track {track_id}")
            try:
                fresh_url = await self._url_provider.get_streaming_url(track_id)
                track.qobuz_url = fresh_url
                track.url_fetched_at = time.time()
            except Exception as e:
                logger.error(f"Failed to refresh URL for track {track_id}: {e}")
                return web.Response(status=502, text="Failed to refresh streaming URL")

        # Forward request to Qobuz CDN
        return await self._proxy_stream(request, track)

    async def _proxy_stream(
        self,
        request: web.Request,
        track: RegisteredTrack,
    ) -> web.StreamResponse:
        """Proxy the audio stream from Qobuz CDN."""
        # Build headers for upstream request
        headers: Dict[str, str] = {}

        # Forward Range header for seeking support
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
            logger.debug(f"Proxying with Range: {range_header}")

        # Create a fresh session for each request (like reference implementation)
        # This avoids connection pooling issues with long-running streams
        timeout = ClientTimeout(total=None, connect=30)  # No total timeout for streaming

        try:
            logger.debug(
                f"Connecting to upstream URL for track {track.track_id}: {track.qobuz_url[:100]}..."
            )
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    track.qobuz_url,
                    headers=headers,
                ) as upstream_response:
                    # Determine response status
                    if upstream_response.status == 206:
                        status = 206  # Partial Content
                    elif upstream_response.status == 200:
                        status = 200
                    else:
                        logger.warning(
                            f"Upstream error for track {track.track_id}: {upstream_response.status}"
                        )
                        return web.Response(
                            status=502, text=f"Upstream error: {upstream_response.status}"
                        )

                    # Build response headers
                    response_headers: Dict[str, str] = {
                        "Content-Type": track.content_type,
                        "Accept-Ranges": "bytes",
                    }

                    # Forward content headers
                    if "Content-Length" in upstream_response.headers:
                        response_headers["Content-Length"] = upstream_response.headers[
                            "Content-Length"
                        ]
                    if "Content-Range" in upstream_response.headers:
                        response_headers["Content-Range"] = upstream_response.headers[
                            "Content-Range"
                        ]

                    logger.debug(f"Streaming track {track.track_id}, headers: {response_headers}")

                    # Create streaming response
                    response = web.StreamResponse(
                        status=status,
                        headers=response_headers,
                    )
                    await response.prepare(request)

                    # Stream chunks to client
                    bytes_sent = 0
                    async for chunk in upstream_response.content.iter_chunked(STREAM_CHUNK_SIZE):
                        try:
                            await response.write(chunk)
                            bytes_sent += len(chunk)
                        except (ConnectionResetError, ConnectionError):
                            logger.debug(
                                f"Client disconnected after {bytes_sent} bytes for track {track.track_id}"
                            )
                            return response

                    await response.write_eof()
                    logger.debug(
                        f"Finished streaming track {track.track_id}, sent {bytes_sent} bytes"
                    )
                    return response

        except asyncio.CancelledError:
            logger.debug(f"Stream cancelled for track {track.track_id}")
            raise
        except (ConnectionResetError, ConnectionError) as e:
            # Client disconnected - this is normal when Sonos probes or seeks
            logger.debug(f"Client connection closed for track {track.track_id}: {type(e).__name__}")
            return web.Response(status=499, text="Client closed connection")
        except Exception as e:
            logger.error(f"Proxy error for track {track.track_id}: {type(e).__name__}: {e}")
            logger.error(f"URL was: {track.qobuz_url[:100]}...")
            import traceback

            logger.debug(f"Full traceback: {traceback.format_exc()}")
            return web.Response(status=502, text=f"Proxy error: {e}")

    def _get_local_ip(self) -> str:
        """Get local IP address for proxy URL."""
        try:
            # Connect to external address to determine local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
