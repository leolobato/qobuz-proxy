"""
Qobuz API Client.

Handles authentication, session management, and signed API requests.
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import unquote, urlencode

import aiohttp

from qobuz_proxy.auth.tokens import WSToken

logger = logging.getLogger(__name__)


class QobuzAPIError(Exception):
    """Qobuz API error."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class QobuzAPIClient:
    """Qobuz REST API client with request signing."""

    API_BASE = "https://www.qobuz.com/api.json/0.2"

    def __init__(self, app_id: str, app_secret: str):
        """
        Initialize API client.

        Args:
            app_id: Qobuz application ID
            app_secret: Qobuz application secret
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.user_auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.x_session_id: Optional[str] = None
        self.x_session_expires_at: int = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._has_ws_token: bool = False

    async def __aenter__(self) -> "QobuzAPIClient":
        """Async context manager entry."""
        headers = {
            "User-Agent": "Mozilla/5.0",
            "X-App-Id": self.app_id,
        }
        self._session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Async context manager exit."""
        if self._session:
            await self._session.close()
            self._session = None

    async def login_with_token(self, user_id: str, auth_token: str) -> bool:
        """Validate a cached user auth token against the Qobuz API.

        Used on startup to check if a previously saved token is still valid.
        On success, stores the (potentially refreshed) token from the response.
        """
        try:
            url = f"{self.API_BASE}/user/login"
            headers = {
                "X-App-Id": self.app_id,
                "X-User-Auth-Token": auth_token,
                "Content-Type": "text/plain;charset=UTF-8",
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data="extra=partner", headers=headers, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            f"Login validation failed: HTTP {resp.status} — {body[:200]}"
                        )
                        return False
                    response = await resp.json()

            if response and "user_auth_token" in response:
                self.user_auth_token = response["user_auth_token"]
                self.user_id = user_id
                logger.info(f"Logged in as user {self.user_id}")
                return True

        except Exception as e:
            logger.error(f"Login failed: {e}")

        return False

    async def start_session(self) -> bool:
        """
        Start a Qobuz streaming session.

        Returns:
            True if successful
        """
        now_ms = int(time.time() * 1000)
        if self.x_session_id and self.x_session_expires_at > now_ms + 60000:
            return True  # Session still valid

        try:
            request_ts = f"{time.time():.6f}"
            params = {"profile": "qbz-1"}

            # Build signature: "sessionstart" + sorted key-value pairs + timestamp + secret
            sig_string = "sessionstart"
            for key in sorted(params.keys()):
                sig_string += key + str(params[key])
            sig_string += request_ts + self.app_secret
            signature = hashlib.md5(sig_string.encode()).hexdigest()

            url = f"{self.API_BASE}/session/start"
            body = f"profile=qbz-1&request_ts={request_ts}&request_sig={signature}"

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-App-Id": self.app_id,
            }
            if self.user_auth_token:
                headers["X-User-Auth-Token"] = self.user_auth_token

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        response = await resp.json()
                        if "session_id" in response:
                            self.x_session_id = response["session_id"]
                            self.x_session_expires_at = response.get("expires_at", 0) * 1000
                            logger.debug("Session started")
                            return True
                    else:
                        body_text = await resp.text()
                        logger.warning(
                            f"Session start failed: HTTP {resp.status} — {body_text[:200]}"
                        )

        except Exception as e:
            logger.error(f"Failed to start session: {e}")

        return False

    async def get_ws_token(self) -> Optional[WSToken]:
        """
        Mint a Qobuz Connect WebSocket token (qws/createToken or qws/refreshToken).

        The Qobuz app supplies a WS token when it connects to the device, but
        those tokens are only valid for ~60 minutes. Minting our own lets the
        device stay connected through long playback sessions without waiting
        for the app to reconnect.

        Returns:
            WSToken on success, None on failure
        """
        if not self.user_auth_token:
            logger.debug("Cannot mint WS token without user auth token")
            return None

        await self.start_session()

        headers = {
            "Referer": "https://play.qobuz.com/",
            "Origin": "https://play.qobuz.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-App-Id": self.app_id,
            "X-User-Auth-Token": self.user_auth_token,
        }
        if self.x_session_id:
            headers["X-Session-Id"] = self.x_session_id

        action = "refreshToken" if self._has_ws_token else "createToken"
        url = f"{self.API_BASE}/qws/{action}"

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data="jwt=jwt_qws", headers=headers, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"qws/{action} failed: HTTP {resp.status} — {body[:200]}")
                        return None
                    data = await resp.json()

            tok = data.get("jwt_qws") or {}
            token = WSToken(
                jwt=tok.get("jwt", ""),
                exp_s=int(tok.get("exp", 0)),
                endpoint=unquote(tok.get("endpoint", "")),
            )
            if not token.is_valid():
                logger.warning(f"qws/{action} returned an incomplete token")
                return None

            self._has_ws_token = True
            validity_min = max(0, token.exp_s - int(time.time())) // 60
            logger.info(f"Minted WebSocket token via qws/{action} (valid ~{validity_min} min)")
            return token

        except Exception as e:
            logger.error(f"Failed to mint WS token: {e}")
            return None

    async def get_track_url(self, track_id: str, quality: int = 27) -> Optional[dict[str, Any]]:
        """
        Get streaming URL and format info for a track.

        Args:
            track_id: Track ID
            quality: Audio quality (5, 6, 7, or 27)

        Returns:
            Dict with 'url', 'format_id', 'bit_depth', 'sampling_rate',
            'mime_type' keys, or None on failure
        """
        if not await self.start_session():
            logger.debug("Session start failed; attempting track URL without session")

        try:
            request_ts = f"{time.time():.6f}"
            format_id = str(quality)

            # Build signature for track/getFileUrl
            sig_string = (
                f"trackgetFileUrlformat_id{format_id}intentstream"
                f"track_id{track_id}{request_ts}{self.app_secret}"
            )
            signature = hashlib.md5(sig_string.encode()).hexdigest()

            params = {
                "format_id": format_id,
                "intent": "stream",
                "track_id": track_id,
                "request_ts": request_ts,
                "request_sig": signature,
            }

            url = f"{self.API_BASE}/track/getFileUrl?{urlencode(params)}"
            headers: dict[str, str] = {
                "X-App-Id": self.app_id,
            }
            if self.user_auth_token:
                headers["X-User-Auth-Token"] = self.user_auth_token
            if self.x_session_id:
                headers["X-Session-Id"] = self.x_session_id

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        url_result = data.get("url")
                        if url_result:
                            return {
                                "url": url_result,
                                "format_id": data.get("format_id", quality),
                                "bit_depth": data.get("bit_depth", 0),
                                "sampling_rate": data.get("sampling_rate", 0),
                                "mime_type": data.get("mime_type", ""),
                                # Opaque token needed by track/reportStreamingEndJson.
                                "blob": data.get("blob", ""),
                            }
                        return None
                    else:
                        body = await resp.text()
                        logger.error(f"Failed to get track URL: {resp.status} — {body[:200]}")

        except Exception as e:
            logger.error(f"Failed to get track URL: {e}")

        return None

    def _report_headers(self) -> dict[str, str]:
        """Auth headers shared by the streaming-report endpoints."""
        headers = {"X-App-Id": self.app_id}
        if self.user_auth_token:
            headers["X-User-Auth-Token"] = self.user_auth_token
        if self.x_session_id:
            headers["X-Session-Id"] = self.x_session_id
        return headers

    async def report_streaming_start(self, *, track_id: str, format_id: int) -> bool:
        """Tell Qobuz a track started playing (track/reportStreamingStart).

        This registers the play with Qobuz, which in turn powers listening
        history and Last.fm "now playing"/scrobbling for linked accounts. The
        body mirrors the official client: a form field ``events`` holding a
        one-element JSON array. The call is unsigned; auth is via headers.
        """
        await self.start_session()
        event = {
            "user_id": int(self.user_id) if self.user_id else 0,
            "track_id": int(track_id),
            "format_id": int(format_id),
            "date": int(time.time()),
            "duration": 0,
            "online": True,
            "local": False,
        }
        body = "events=" + json.dumps([event], separators=(",", ":"))
        headers = self._report_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return await self._post_report(
            f"{self.API_BASE}/track/reportStreamingStart", body, headers, "start"
        )

    async def report_streaming_end(
        self,
        *,
        track_id: str,
        blob: str,
        context_uuid: Optional[str],
        started_at_ms: int,
        played_seconds: int,
    ) -> bool:
        """Tell Qobuz a track finished playing (track/reportStreamingEndJson).

        The end event carries how long the track was actually played, which is
        what Qobuz uses to decide whether the play counts (and scrobbles). The
        ``blob`` comes from the track/getFileUrl response and ``context_uuid``
        from the play queue.
        """
        await self.start_session()
        event: dict[str, Any] = {
            "blob": blob,
            "track_context_uuid": context_uuid or "",
            "start_stream": self._iso8601_ms(started_at_ms),
            "online": True,
            "local": False,
            "duration": int(played_seconds),
        }
        from qobuz_proxy import __version__

        payload = {
            "events": [event],
            "renderer_context": {"software_version": f"qobuz-proxy-{__version__}"},
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._report_headers()
        headers["Content-Type"] = "application/json"
        return await self._post_report(
            f"{self.API_BASE}/track/reportStreamingEndJson", body, headers, f"end track {track_id}"
        )

    async def _post_report(self, url: str, body: str, headers: dict[str, str], what: str) -> bool:
        """POST a streaming-report body; log and swallow failures (best-effort)."""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
                    # Qobuz answers reportStreamingStart with 201 Created (body
                    # still says success), so accept any 2xx.
                    if 200 <= resp.status < 300:
                        logger.debug(f"Streaming report ({what}) ok: HTTP {resp.status}")
                        return True
                    text = await resp.text()
                    logger.warning(
                        f"Streaming report ({what}) failed: HTTP {resp.status} — {text[:200]}"
                    )
                    return False
        except Exception as e:
            logger.warning(f"Streaming report ({what}) error: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _iso8601_ms(epoch_ms: int) -> str:
        """Format epoch milliseconds as ISO8601 UTC with millis and a Z suffix."""
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    async def get_track_metadata(self, track_id: str) -> Optional[dict[str, Any]]:
        """
        Get track metadata.

        Args:
            track_id: Track ID

        Returns:
            Track metadata dict or None
        """
        try:
            params = {"track_id": track_id, "app_id": self.app_id}
            response = await self._request_signed("track", "get", params=params)

            if not response:
                return None

            # Transform to flat format
            metadata: dict[str, Any] = {
                "title": response.get("title", ""),
                "artist": "",
                "album": "",
                "album_art_url": "",
                "duration_ms": int(response.get("duration", 0)) * 1000,
            }

            performer = response.get("performer")
            if performer and isinstance(performer, dict):
                metadata["artist"] = performer.get("name", "")

            album = response.get("album")
            if album and isinstance(album, dict):
                metadata["album"] = album.get("title", "")
                image = album.get("image")
                if image and isinstance(image, dict):
                    metadata["album_art_url"] = image.get("large") or image.get("small") or ""

            return metadata

        except Exception as e:
            logger.error(f"Failed to get track metadata: {e}")

        return None

    async def _request_signed(
        self,
        obj: str,
        action: str,
        params: Optional[dict[str, Any]] = None,
        method: str = "GET",
        body: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Make a signed API request."""
        if params is None:
            params = {}

        request_ts = f"{time.time():.6f}"

        # Build signature
        sig_string = obj + action
        for key in sorted(params.keys()):
            sig_string += key + str(params[key])
        sig_string += request_ts + self.app_secret
        signature = hashlib.md5(sig_string.encode()).hexdigest()

        params["request_ts"] = request_ts
        params["request_sig"] = signature

        url = f"{self.API_BASE}/{obj}/{action}?{urlencode(params)}"

        try:
            session = self._session
            close_session = False
            if session is None:
                headers: dict[str, str] = {
                    "X-App-Id": self.app_id,
                    "User-Agent": "Mozilla/5.0",
                }
                if self.user_auth_token:
                    headers["X-User-Auth-Token"] = self.user_auth_token
                session = aiohttp.ClientSession(headers=headers)
                close_session = True

            timeout = aiohttp.ClientTimeout(total=10)
            try:
                if method == "POST":
                    async with session.post(url, data=body, timeout=timeout) as resp:
                        if resp.status == 200:
                            result: dict[str, Any] = await resp.json()
                            return result
                        else:
                            logger.warning(f"API request failed: {resp.status}")
                else:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            return result
                        else:
                            logger.warning(f"API request failed: {resp.status}")
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.error(f"API request error: {e}")

        return None
