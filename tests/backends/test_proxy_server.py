"""Tests for AudioProxyServer upstream retry behavior."""

import socket
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import web

from qobuz_proxy.backends.dlna.proxy_server import AudioProxyServer

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB deterministic payload
ABORT_AFTER = 100_000


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FlakyUpstream:
    """Fake CDN that aborts the first full-body request mid-stream.

    Subsequent requests (e.g. Range resumes) are served completely, mimicking
    the Akamai behavior seen in issue #10 where a long-running stream dies
    with a short read but a fresh ranged request succeeds.
    """

    def __init__(self):
        self.range_headers: list = []  # Range header of each request (None = full body)
        self._aborted_once = False
        self._runner = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        rng = request.headers.get("Range")
        self.range_headers.append(rng)

        start = 0
        if rng:
            start = int(rng.removeprefix("bytes=").split("-")[0])
        body = PAYLOAD[start:]

        if rng is None and not self._aborted_once:
            self._aborted_once = True
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Length": str(len(body)), "Accept-Ranges": "bytes"},
            )
            await resp.prepare(request)
            await resp.write(body[:ABORT_AFTER])
            # Kill the connection without delivering the advertised length
            assert request.transport is not None
            request.transport.close()
            return resp

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(len(body))}
        status = 200
        if rng:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
        return web.Response(status=status, body=body, headers=headers)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/file", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        port = _free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        return f"http://127.0.0.1:{port}/file"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


class MethodRecordingUpstream:
    """Fake CDN that records the HTTP method of every request."""

    def __init__(self):
        self.methods: list = []
        self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        self.methods.append(request.method)
        return web.Response(
            status=200,
            body=PAYLOAD,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(len(PAYLOAD))},
        )

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/file", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        port = _free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        return f"http://127.0.0.1:{port}/file"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


async def test_head_probe_does_not_download_the_track():
    """Regression for BUG-29: a renderer HEAD probe must be answered from
    upstream headers, not by streaming (and discarding) the whole file."""
    upstream = MethodRecordingUpstream()
    upstream_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(return_value=upstream_url)

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    await proxy.start()
    try:
        proxy.register_track("42", upstream_url, "audio/flac")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(f"http://127.0.0.1:{port}/audio/42.flac") as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "audio/flac"
                assert resp.headers["Accept-Ranges"] == "bytes"
                # Renderers size their buffering from the probe's Content-Length
                assert resp.headers["Content-Length"] == str(len(PAYLOAD))
    finally:
        await proxy.stop()
        await upstream.stop()

    # The CDN saw only a HEAD — never a body-transferring GET
    assert upstream.methods == ["HEAD"]


class ExpiredUrlUpstream:
    """Fake CDN that rejects stale signed URLs with 403 but serves fresh ones."""

    def __init__(self):
        self.requests: list = []  # token query param of each request
        self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        token = request.query.get("token", "")
        self.requests.append(token)
        if token != "fresh":
            return web.Response(status=403, text="URL signature expired")
        return web.Response(
            status=200,
            body=PAYLOAD,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(len(PAYLOAD))},
        )

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/file", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        port = _free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        return f"http://127.0.0.1:{port}/file"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


async def test_refreshes_url_and_retries_on_upstream_403():
    """An expired signed URL (CDN 403) must trigger a forced refresh, not a 502."""
    upstream = ExpiredUrlUpstream()
    base_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(return_value=f"{base_url}?token=fresh")

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    await proxy.start()
    try:
        proxy.register_track("42", f"{base_url}?token=stale", "audio/flac")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/audio/42.flac") as resp:
                assert resp.status == 200
                body = await resp.read()
    finally:
        await proxy.stop()
        await upstream.stop()

    assert body == PAYLOAD
    assert upstream.requests == ["stale", "fresh"]
    provider.get_streaming_url.assert_awaited_once_with("42", force=True)


async def test_returns_502_when_refresh_fails_after_403():
    """If no fresh URL can be fetched, the 403 surfaces as a single 502 (no retry loop)."""
    upstream = ExpiredUrlUpstream()
    base_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(side_effect=RuntimeError("no URL"))

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    await proxy.start()
    try:
        proxy.register_track("42", f"{base_url}?token=stale", "audio/flac")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/audio/42.flac") as resp:
                assert resp.status == 502
    finally:
        await proxy.stop()
        await upstream.stop()

    assert upstream.requests == ["stale"]


async def test_resumes_upstream_after_midstream_failure():
    """A mid-stream upstream failure must not kill the renderer's stream."""
    upstream = FlakyUpstream()
    upstream_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(return_value=upstream_url)

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    await proxy.start()
    body = b""
    try:
        proxy.register_track("42", upstream_url, "audio/flac")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/audio/42.flac") as resp:
                assert resp.status == 200
                try:
                    body = await resp.read()
                except (aiohttp.ClientPayloadError, aiohttp.ServerTimeoutError, TimeoutError):
                    pass  # truncated/stalled stream — the assertion below reports it
    finally:
        await proxy.stop()
        await upstream.stop()

    assert body == PAYLOAD
    # The proxy must have resumed with a Range request, not restarted from zero
    assert len(upstream.range_headers) == 2
    resume = upstream.range_headers[1]
    assert resume is not None and resume.startswith("bytes=")
    assert int(resume.removeprefix("bytes=").split("-")[0]) > 0


async def test_reports_delivery_failure_when_renderer_gets_no_audio():
    """A 502 before any audio tells the backend the renderer has nothing to play."""
    upstream = ExpiredUrlUpstream()
    base_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(side_effect=RuntimeError("no URL"))

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    failures: list = []
    proxy.set_delivery_failure_callback(
        lambda key, before_audio: failures.append((key, before_audio))
    )
    await proxy.start()
    try:
        proxy.register_track("42", f"{base_url}?token=stale", "audio/flac", proxy_key="42_3")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/audio/42_3.flac") as resp:
                assert resp.status == 502
    finally:
        await proxy.stop()
        await upstream.stop()

    assert failures == [("42_3", True)]


async def test_recovered_midstream_failure_is_not_a_delivery_failure():
    upstream = FlakyUpstream()
    upstream_url = await upstream.start()

    provider = MagicMock()
    provider.get_streaming_url = AsyncMock(return_value=upstream_url)

    port = _free_port()
    proxy = AudioProxyServer(url_provider=provider, host="127.0.0.1", port=port)
    failures: list = []
    proxy.set_delivery_failure_callback(
        lambda key, before_audio: failures.append((key, before_audio))
    )
    await proxy.start()
    try:
        proxy.register_track("42", upstream_url, "audio/flac")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/audio/42.flac") as resp:
                assert await resp.read() == PAYLOAD
    finally:
        await proxy.stop()
        await upstream.stop()

    assert failures == []
