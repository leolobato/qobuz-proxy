"""
Qobuz credential scraper.

Extracts app_id and app_secret from play.qobuz.com JavaScript bundles.
Based on StreamCore32's QobuzConfig.cpp.
"""

import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cache location
CACHE_DIR = Path.home() / ".qobuz-proxy"
CACHE_FILE = CACHE_DIR / "credentials.json"


class CredentialScraper:
    """Scrapes Qobuz web player for app credentials."""

    ENTRY_URLS = [
        "https://play.qobuz.com/login",
        "https://play.qobuz.com/",
    ]
    MAX_BUNDLES = 12
    TIMEOUT = 15

    async def fetch_credentials(self) -> Optional[dict[str, Any]]:
        """
        Fetch app_id and app_secret from Qobuz web player.

        Returns:
            Dict with 'app_id' and 'app_secrets', or None if failed
        """
        logger.info("Fetching Qobuz credentials from web player...")

        async with aiohttp.ClientSession() as session:
            for entry_url in self.ENTRY_URLS:
                try:
                    result = await self._try_scrape(session, entry_url)
                    if result:
                        return result
                except Exception as e:
                    logger.warning(f"Failed to scrape {entry_url}: {e}")
                    continue

        logger.error("Failed to fetch credentials from all entry points")
        return None

    async def _try_scrape(
        self, session: aiohttp.ClientSession, entry_url: str
    ) -> Optional[dict[str, Any]]:
        """Try to scrape credentials from a single entry URL."""
        logger.debug(f"Fetching {entry_url}")

        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)
        async with session.get(entry_url, timeout=timeout) as response:
            if response.status != 200:
                return None
            html = await response.text()

        # Extract script URLs
        script_urls = self._extract_scripts(html, entry_url)
        logger.debug(f"Found {len(script_urls)} JavaScript bundles")

        if not script_urls:
            return None

        result: dict[str, Any] = {"app_id": "", "seeds": {}, "secrets": {}}

        for js_url in script_urls[: self.MAX_BUNDLES]:
            try:
                async with session.get(js_url, timeout=timeout) as response:
                    if response.status != 200:
                        continue
                    js_content = await response.text()

                # Scan for app_id
                if not result["app_id"]:
                    app_id = self._scan_app_id(js_content)
                    if app_id:
                        result["app_id"] = app_id
                        logger.debug(f"Found app_id: {app_id}")

                # Scan for seeds
                seeds = self._scan_seeds(js_content)
                if seeds:
                    result["seeds"].update(seeds)

                # Derive secrets from seeds
                if result["seeds"]:
                    secrets = self._derive_secrets(js_content, result["seeds"])
                    if secrets:
                        result["secrets"].update(secrets)

            except Exception as e:
                logger.debug(f"Error scanning {js_url}: {e}")
                continue

        # If we have app_id and secrets, return them
        if result["app_id"] and result["secrets"]:
            return {
                "app_id": result["app_id"],
                "app_secrets": result["secrets"],
            }

        return None

    def _extract_scripts(self, html: str, base_url: str) -> list[str]:
        """Extract JavaScript bundle URLs from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        scripts: list[str] = []

        for script in soup.find_all("script", src=True):
            src = str(script["src"])
            if self._is_player_asset(src):
                scripts.append(self._absolutize(base_url, src))

        for link in soup.find_all("link", rel="preload", attrs={"as": "script"}):
            if "href" in link.attrs:
                href = str(link["href"])
                if self._is_player_asset(href):
                    scripts.append(self._absolutize(base_url, href))

        return list(dict.fromkeys(scripts))  # Remove duplicates

    def _is_player_asset(self, url: str) -> bool:
        """Check if URL is a Qobuz player asset."""
        if not url or url.startswith("data:") or ".js" not in url:
            return False
        if url.startswith("http"):
            return "play.qobuz.com" in url
        return True

    def _absolutize(self, base: str, url: str) -> str:
        """Convert relative URL to absolute."""
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(base, url)

    def _scan_app_id(self, js_content: str) -> Optional[str]:
        """Scan for app_id pattern."""
        match = re.search(r'production:\{api:\{appId:"(\d{9})"', js_content)
        return match.group(1) if match else None

    def _scan_seeds(self, js_content: str) -> dict[str, str]:
        """Scan for seed patterns."""
        seeds: dict[str, str] = {}
        pattern = r'\.initialSeed\("([^"]+)",window\.utimezone\.(\w+)\)'
        for match in re.finditer(pattern, js_content):
            seed = match.group(1)
            timezone = match.group(2).capitalize()
            seeds[timezone] = seed
        return seeds

    def _derive_secrets(self, js_content: str, seeds: dict[str, str]) -> dict[str, str]:
        """Derive secrets from seeds and info/extras."""
        secrets: dict[str, str] = {}
        for timezone, seed in seeds.items():
            pattern = (
                rf"/{timezone}[^{{}}]*?"
                rf'info:\s*["\']([^"\']+)["\'][^{{}}]*?'
                rf'extras:\s*["\']([^"\']+)["\']'
            )
            match = re.search(pattern, js_content, re.IGNORECASE)
            if match:
                info = match.group(1)
                extras = match.group(2)
                combined = seed + info + extras
                if len(combined) > 44:
                    to_decode = combined[:-44]
                    try:
                        secret = self._base64url_decode(to_decode)
                        secrets[timezone] = secret
                    except Exception:
                        pass
        return secrets

    def _base64url_decode(self, s: str) -> str:
        """Decode base64url string."""
        padding = 4 - (len(s) % 4)
        if padding != 4:
            s += "=" * padding
        s = s.replace("-", "+").replace("_", "/")
        return base64.b64decode(s).decode("utf-8", errors="replace")


def load_cached_credentials() -> Optional[dict[str, str]]:
    """Load credentials from cache file."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                creds: dict[str, str] = json.load(f)
                if creds.get("app_id") and creds.get("app_secret"):
                    logger.info(f"Loaded credentials from cache: {CACHE_FILE}")
                    return creds
    except Exception as e:
        logger.warning(f"Failed to load cached credentials: {e}")
    return None


def save_credentials_to_cache(credentials: dict[str, str]) -> bool:
    """Save credentials to cache file."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(credentials, f, indent=2)
        logger.info(f"Cached credentials to {CACHE_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to cache credentials: {e}")
        return False


async def test_secret(app_id: str, app_secret: str) -> bool:
    """Test if app_id/app_secret pair works."""
    test_track_id = "64868955"
    ts = f"{time.time():.6f}"

    params_str = f"format_id5intentstreamtrack_id{test_track_id}"
    sig_str = f"trackgetFileUrl{params_str}{ts}{app_secret}"
    sig = hashlib.md5(sig_str.encode()).hexdigest()

    url = (
        f"https://www.qobuz.com/api.json/0.2/track/getFileUrl?"
        f"format_id=5&intent=stream&track_id={test_track_id}&"
        f"request_ts={ts}&request_sig={sig}"
    )

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0",
                "X-App-Id": app_id,
                "Referer": "https://play.qobuz.com/",
            }
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                # 200, 401, 403 all indicate valid credentials
                return resp.status in (200, 401, 403)
    except Exception:
        return False


async def auto_fetch_credentials(use_cache: bool = True) -> Optional[dict[str, str]]:
    """
    Automatically fetch Qobuz credentials with caching.

    Args:
        use_cache: Try cache first (default: True)

    Returns:
        Dict with 'app_id' and 'app_secret', or None
    """
    if use_cache:
        cached = load_cached_credentials()
        if cached:
            return cached

    logger.info("Fetching credentials from web player...")
    scraper = CredentialScraper()
    result = await scraper.fetch_credentials()

    if not result or "app_id" not in result:
        return None

    app_id = result["app_id"]
    secrets = result.get("app_secrets", {})

    if not secrets:
        return None

    # Test each secret until one works
    logger.info(f"Testing {len(secrets)} secret(s)...")
    for timezone, secret in secrets.items():
        logger.debug(f"Testing {timezone} secret...")
        if await test_secret(app_id, secret):
            logger.info(f"Secret for {timezone} works!")
            credentials = {"app_id": app_id, "app_secret": secret}
            save_credentials_to_cache(credentials)
            return credentials

    logger.error("No working secret found")
    return None
