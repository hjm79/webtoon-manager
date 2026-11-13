"""One-origin viewer forwarding preserves HTTP contracts without changing saved settings."""
import asyncio
import gzip
import json
import os
from unittest.mock import patch

import aiohttp
from aiohttp import web
import httpx

from app import db, repository, viewer_connection, viewer_scan, webtoon_server_client
from app.config import get_settings
db.get_connection()
from app import main
from app.api import viewer_proxy


async def run():
    calls = []
    compressed = gzip.compress(b"viewer compressed content")

    async def upstream(request):
        body = await request.read()
        calls.append({"raw_path": request.raw_path, "method": request.method,
                      "headers": dict(request.headers), "body": body})
        if request.path == "/partial":
            return web.Response(status=206, body=b"part", headers={"Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes", "ETag": '"etag"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
        if request.path == "/compressed":
            return web.Response(body=compressed, headers={"Content-Encoding": "gzip", "Content-Type": "text/html"})
        if request.path == "/redirect":
            return web.Response(status=303, headers={"Location": "/login?next=%2Fviewer%2F"})
        if request.path == "/absolute-redirect":
            return web.Response(status=307, headers={"Location": str(request.url.origin()) + "/p/profile/"})
        if request.path == "/cookies":
            response = web.Response(status=303, headers={"Location": "/viewer/"})
            response.headers.add("Set-Cookie", "viewer_session=abc; Path=/viewer/; HttpOnly; SameSite=Lax")
            response.headers.add("Set-Cookie", "viewer_device=def; Path=/viewer/; HttpOnly")
            return response
        if request.path == "/auth":
            return web.Response(status=401, headers={"WWW-Authenticate": "Bearer"})
        if request.path == "/large":
            return web.Response(body=b"12345")
        if request.path == "/chunked-large":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"123")
            await response.write(b"45")
            await response.write_eof()
            return response
        if request.path == "/api/lookup/latest":
            url = "https://books.example/viewer/reader.html?series=untrusted&token=discarded" if request.query.get("series") == "public" else "http://internal/reader.html?series=series1"
            return web.json_response({"series_id": "series1", "chapter_id": "chapter1", "url": url})
        if request.path == "/api/integration/rescan":
            return web.json_response({"started": True}) if request.headers.get("X-Integration-Token") == "scan-secret" else web.Response(status=401)
        return web.json_response({"raw_path": request.raw_path, "body": body.decode(), "method": request.method})

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", upstream)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    repository.set_setting("webtoon_server_url", "http://old-viewer:8231")
    original = repository.get_setting("webtoon_server_url")
    env = {"VIEWER_INTERNAL_URL": base, "VIEWER_PUBLIC_PATH": "/viewer"}
    try:
        with patch.dict(os.environ, env):
            get_settings.cache_clear()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://manager.test") as client:
                config = (await client.get("/api/viewer/config")).json()
                assert config == {"enabled": True, "public_path": "/viewer", "library_url": "/viewer/", "reader_url": "/viewer/reader.html"}
                assert base not in json.dumps(config)
                settings = (await client.get("/api/settings/webtoon-server")).json()
                assert settings == {"webtoon_server_url": "/viewer", "integrated": True, "read_only": True}
                assert (await client.post("/api/settings/webtoon-server", json={"webtoon_server_url": "http://changed"})).status_code == 409
                assert repository.get_setting("webtoon_server_url") == original
                redirect = await client.get("/viewer?x=1")
                assert redirect.status_code == 307 and redirect.headers["location"] == "/viewer/?x=1"
                assert (await client.get("/viewer?x=" + "a" * 16384)).status_code == 414

                response = await client.get("/viewer/echo/a%2Fb?x=a%2Bb&target=https%3A%2F%2Fother.example", headers={
                    "Range": "bytes=2-5", "If-Range": '"etag"', "If-None-Match": '"etag"',
                    "If-Modified-Since": "Mon, 01 Jan 2024 00:00:00 GMT", "Cookie": "viewer_session=abc",
                    "Authorization": "Bearer browser", "Connection": "X-Remove", "X-Remove": "not-forwarded",
                    "X-Forwarded-Host": "spoofed.example"})
                assert response.status_code == 200
                assert calls[-1]["raw_path"] == "/echo/a%2Fb?x=a%2Bb&target=https%3A%2F%2Fother.example"
                headers = calls[-1]["headers"]
                assert headers["Range"] == "bytes=2-5" and headers["If-Range"] == '"etag"'
                assert headers["If-None-Match"] == '"etag"' and "If-Modified-Since" in headers
                assert headers["Cookie"] == "viewer_session=abc" and headers["Authorization"] == "Bearer browser"
                assert "X-Remove" not in headers and headers["X-Forwarded-Host"] == "manager.test"
                assert headers["X-Forwarded-Prefix"] == "/viewer" and headers["X-Forwarded-Proto"] == "http"

                partial = await client.get("/viewer/partial")
                assert partial.status_code == 206 and partial.content == b"part"
                assert partial.headers["content-range"] == "bytes 2-5/10" and partial.headers["etag"] == '"etag"'
                head = await client.head("/viewer/partial")
                assert head.status_code == 206 and not head.content and head.headers["content-length"] == "4"
                async with client.stream("GET", "/viewer/compressed") as response:
                    raw = b"".join([chunk async for chunk in response.aiter_raw()])
                    assert raw == compressed and response.headers["content-encoding"] == "gzip"
                redirected = await client.get("/viewer/redirect")
                assert redirected.status_code == 303 and redirected.headers["location"] == "/viewer/login?next=%2Fviewer%2F"
                assert calls[-1]["raw_path"] == "/redirect"
                absolute = await client.get("/viewer/absolute-redirect")
                assert absolute.status_code == 307 and absolute.headers["location"] == "/viewer/p/profile/"
                cookies = await client.get("/viewer/cookies")
                assert len(cookies.headers.get_list("set-cookie")) == 2 and cookies.headers["location"] == "/viewer/"
                denied = await client.get("/viewer/auth")
                assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
                assert (await client.post("/viewer/echo", content=b"payload")).json()["body"] == "payload"

                count = len(calls)
                with patch.object(viewer_proxy, "MAX_REQUEST_BYTES", 4):
                    assert (await client.post("/viewer/echo", content=b"12345")).status_code == 413

                    async def chunks():
                        yield b"123"
                        yield b"45"
                    assert (await client.post("/viewer/echo", content=chunks())).status_code == 413
                assert len(calls) == count
                with patch.object(viewer_proxy, "MAX_RESPONSE_BYTES", 4):
                    assert (await client.get("/viewer/large")).status_code == 502
                    sessions = []
                    session_class = aiohttp.ClientSession

                    def recording_session(*args, **kwargs):
                        session = session_class(*args, **kwargs)
                        sessions.append(session)
                        return session

                    def cap_error(exc):
                        return "Viewer response exceeded" in str(exc) or any(cap_error(e) for e in getattr(exc, "exceptions", []))

                    with patch.object(viewer_proxy.aiohttp, "ClientSession", recording_session):
                        try:
                            await client.get("/viewer/chunked-large")
                        except Exception as exc:
                            assert cap_error(exc), repr(exc)
                        else:
                            raise AssertionError("Unknown-length responses must stop at the streaming limit")
                    assert sessions and all(s.closed for s in sessions)
                assert (await client.post("/viewer/api/integration/rescan")).status_code == 401

                result = (await client.get("/api/reader/status", params={"title": "작품"})).json()
                assert result["url"] == "/viewer/reader.html?series=series1" and result["status"] == "ready"
                async with aiohttp.ClientSession() as session:
                    assert await webtoon_server_client.fetch_reader_url(session, original, "작품", 5) == "/viewer/reader.html?series=series1&chapter=chapter1"
                    assert await webtoon_server_client.fetch_reader_url(session, original, "public", 5) == "https://books.example/viewer/reader.html?series=series1&chapter=chapter1"
                    assert await webtoon_server_client.start_scan(session, original, "scan-secret", 5) == "started"
                for candidate in (base + "/viewer/reader.html", "https://user:secret@books.example/viewer/reader.html", "https://books.example/reader.html", "//books.example/viewer/reader.html", "https://books.example:invalid/viewer/reader.html"):
                    assert viewer_connection.report_reader_url(candidate, "series1") == "/viewer/reader.html?series=series1"
                viewer_scan.set_config(enabled=True, token="scan-secret")
                repository.set_setting("webtoon_server_url", None)
                assert viewer_scan.is_ready()
                repository.set_setting("webtoon_server_url", original)

                for bad in ("https://user:secret@example.com", base + "/path", base + "?token=secret", "//example.com"):
                    with patch.dict(os.environ, {"VIEWER_INTERNAL_URL": bad}):
                        get_settings.cache_clear()
                        response = await client.get("/viewer/")
                        assert response.status_code == 503 and "secret" not in response.text

        get_settings.cache_clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://manager.test") as client:
            assert (await client.get("/viewer/")).status_code == 404
            assert (await client.get("/api/viewer/config")).json()["enabled"] is False
            assert (await client.get("/api/settings/webtoon-server")).json() == {"webtoon_server_url": original}
        assert repository.get_setting("webtoon_server_url") == original
    finally:
        get_settings.cache_clear()
        await runner.cleanup()


async def disconnected_before_stream():
    class Upstream:
        status = 200
        closed = False
        def close(self): self.closed = True

    class Session:
        closed = False
        async def close(self): self.closed = True

    async def body():
        yield b"unread"

    upstream, session = Upstream(), Session()
    response = viewer_proxy.ViewerResponse(body(), upstream=upstream, session=session)

    async def send(_): raise OSError("client disconnected before response headers")
    async def receive(): return {"type": "http.disconnect"}
    try:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    except Exception:
        pass
    assert upstream.closed and session.closed


asyncio.run(run())
asyncio.run(disconnected_before_stream())
print("Viewer proxy HTTP, auth, byte ranges, compression, limits, disconnect cleanup and saved configuration OK")
