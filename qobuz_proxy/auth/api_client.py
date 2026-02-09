"""
Qobuz API Client.

Handles authentication, session management, and signed API requests.
"""

import hashlib
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

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

    async def login(self, email: str, password: str) -> bool:
        """
        Login to Qobuz.

        Args:
            email: User email
            password: User password

        Returns:
            True if successful
        """
        try:
            params = {
                "email": email,
                "password": password,
                "app_id": self.app_id,
            }
            response = await self._request_signed(
                "user", "login", params=params, method="POST", body="extra=partner"
            )

            if response and "user_auth_token" in response:
                self.user_auth_token = response["user_auth_token"]
                self.user_id = str(response.get("user", {}).get("id", ""))
                logger.info(f"Logged in as user {self.user_id}")
                return True

        except Exception as e:
            logger.error(f"Login failed: {e}")

        return False

    async def start_session(self) -> bool:
        """
        Start a Qobuz session (required for API calls).

        Returns:
            True if successful
        """
        now_ms = int(time.time() * 1000)
        if self.x_session_id and self.x_session_expires_at > now_ms + 60000:
            return True  # Session still valid

        try:
            request_ts = f"{time.time():.6f}"
            params = {"profile": "qbz-1"}

            # Build signature
            sig_string = "sessionstart"
            for key in sorted(params.keys()):
                sig_string += key + str(params[key])
            sig_string += request_ts + self.app_secret
            signature = hashlib.md5(sig_string.encode()).hexdigest()

            body = f"profile=qbz-1&request_ts={request_ts}&request_sig={signature}"
            url = f"{self.API_BASE}/session/start"

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://play.qobuz.com/",
                "Origin": "https://play.qobuz.com",
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

        except Exception as e:
            logger.error(f"Failed to start session: {e}")

        return False

    async def get_track_url(self, track_id: str, quality: int = 27) -> Optional[str]:
        """
        Get streaming URL for a track.

        Args:
            track_id: Track ID
            quality: Audio quality (5, 6, 7, or 27)

        Returns:
            Streaming URL or None
        """
        if not await self.start_session():
            logger.error("Failed to start session")
            return None

        try:
            request_ts = f"{time.time():.6f}"
            sign_params = {
                "format_id": str(quality),
                "intent": "stream",
                "track_id": track_id,
            }

            # Build signature
            sig_string = "trackgetFileUrl"
            for key in sorted(sign_params.keys()):
                sig_string += key + str(sign_params[key])
            sig_string += request_ts + self.app_secret
            signature = hashlib.md5(sig_string.encode()).hexdigest()

            params = {
                **sign_params,
                "request_ts": request_ts,
                "request_sig": signature,
            }

            url = f"{self.API_BASE}/track/getFileUrl?{urlencode(params)}"
            headers = {
                "Referer": "https://play.qobuz.com/",
                "Origin": "https://play.qobuz.com",
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
                        url_result: Optional[str] = data.get("url")
                        return url_result
                    else:
                        logger.error(f"Failed to get track URL: {resp.status}")

        except Exception as e:
            logger.error(f"Failed to get track URL: {e}")

        return None

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
                session = aiohttp.ClientSession(
                    headers={"X-App-Id": self.app_id, "User-Agent": "Mozilla/5.0"}
                )
                close_session = True

            timeout = aiohttp.ClientTimeout(total=10)
            try:
                if method == "POST":
                    async with session.post(url, data=body, timeout=timeout) as resp:
                        if resp.status == 200:
                            result: dict[str, Any] = await resp.json()
                            return result
                        else:
                            logger.debug(f"API request failed: {resp.status}")
                else:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            return result
                        else:
                            logger.debug(f"API request failed: {resp.status}")
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.error(f"API request error: {e}")

        return None
