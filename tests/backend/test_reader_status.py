"""Reader readiness differentiates empty libraries from connection/configuration failures."""
import asyncio
import json
from unittest.mock import patch

import httpx

from app import db, repository
db.get_connection()
import app.main as main
from app.api import reader


class Content:
    def __init__(self, data, chunk_size=None):
        self.data = data
        self.offset = 0
        self.chunk_size = chunk_size

    async def read(self, limit):
        count = min(limit, self.chunk_size) if self.chunk_size is not None else limit
        chunk = self.data[self.offset:self.offset + count]
        self.offset += len(chunk)
        return chunk


class Response:
    def __init__(self, status=200, data=None, raw=None, chunk_size=None):
        self.status = status
        self.content = Content(raw if raw is not None else json.dumps(data).encode(), chunk_size)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class Session:
    response = Response(404)
    responses = []
    calls = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        repository.set_setting("webtoon_server_url", None)
        result = (await client.get("/api/reader/status", params={"title": "작품"})).json()
        assert result["status"] == "not_configured" and result["url"] is None
        assert (await client.get("/api/reader/status", params={"title": " ", "platform": "naver"})).status_code == 422
        assert (await client.get("/api/reader/status", params={"title": "작품", "platform": "other"})).status_code == 422
        for bad in ("javascript:alert(1)", "http://user:secret@viewer", "http://viewer?token=secret", "http://viewer\n"):
            repository.set_setting("webtoon_server_url", bad)
            result = (await client.get("/api/reader/status", params={"title": "작품"})).json()
            assert result["status"] == "error" and "secret" not in str(result)
        repository.set_setting("webtoon_server_url", "http://viewer:8000/prefix/")
        with patch.object(reader.aiohttp, "ClientSession", Session):
            for status, expected in ((404, "not_downloaded"), (401, "error"), (403, "error"), (302, "error"), (500, "error")):
                Session.response = Response(status)
                result = (await client.get("/api/reader/status", params={"title": "작품"})).json()
                assert result["status"] == expected and result["url"] is None
            Session.response = Response(200, {"series_id": "series-1", "chapter_id": "chapter_2", "url": "https://untrusted.example/"})
            result = (await client.get("/api/reader/status", params={"title": "제목 : 부제"})).json()
            assert result["status"] == "ready"
            assert result["url"] == "http://viewer:8000/prefix/reader.html?series=series-1"
            assert Session.calls[-1][1]["allow_redirects"] is False
            assert Session.calls[-1][1]["params"]["series"] == "제목 ： 부제"
            assert Session.calls[-1][1]["params"]["platform"] == "naver"
            Session.responses = [Response(404), Response(200, {"series_id": "archive", "chapter_id": "a"})]
            result = (await client.get("/api/reader/status", params={"title": "작품", "platform": "kakao"})).json()
            assert result["status"] == "ready" and "series=archive" in result["url"]
            assert Session.calls[-2][1]["params"]["platform"] == "kakao"
            assert Session.calls[-1][1]["params"] == {"series": "작품"}
            Session.responses = [Response(404), Response(409)]
            result = (await client.get("/api/reader/status", params={"title": "작품"})).json()
            assert result["status"] == "error" and "여러 라이브러리" in result["message"]
            Session.response = Response(200, {"series_id": "chunked", "chapter_id": "a"}, chunk_size=3)
            assert (await client.get("/api/reader/status", params={"title": "작품"})).json()["status"] == "ready"
            for data in ([], {"series_id": "../evil", "chapter_id": "a"}, {"url": "javascript:alert(1)"}):
                Session.response = Response(200, data)
                assert (await client.get("/api/reader/status", params={"title": "작품"})).json()["status"] == "error"
            for raw in (b"invalid JSON", b"x" * 65537):
                Session.response = Response(200, raw=raw)
                assert (await client.get("/api/reader/status", params={"title": "작품"})).json()["status"] == "error"
            Session.response = asyncio.TimeoutError()
            assert (await client.get("/api/reader/status", params={"title": "작품"})).json()["status"] == "error"


asyncio.run(run())
print("Reader configuration, empty library, authentication, safe URL and failure states OK")
