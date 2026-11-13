"""Expose the configured internal viewer without publishing its own host port."""
import asyncio
import logging
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse, StreamingResponse
from yarl import URL

from app import viewer_connection

router = APIRouter()
log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024 * 1024
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade"}
_FORWARDED_HEADERS = {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix"}


def _connection_headers(headers) -> set[str]:
    return {name.strip().lower() for name in headers.get("connection", "").split(",") if name.strip()}


def _location(value: str, base: str, prefix: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    upstream = urlsplit(base)
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (upstream.scheme, upstream.netloc):
            return value
        path = parsed.path
    elif value.startswith("/") and not value.startswith("//"):
        path = parsed.path
    else:
        return value
    if path != prefix and not path.startswith(prefix + "/"):
        path = prefix + (path if path.startswith("/") else "/" + path)
    return urlunsplit(("", "", path, parsed.query, parsed.fragment))


class ViewerResponse(StreamingResponse):
    def __init__(self, content, *, upstream, session):
        super().__init__(content, status_code=upstream.status)
        self.upstream = upstream
        self.session = session

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.upstream.close()
            await self.session.close()


async def forward(request: Request):
    if not viewer_connection.integrated():
        raise HTTPException(404, "통합 뷰어가 설정되지 않았습니다.")
    try:
        base = viewer_connection.internal_base()
        prefix = viewer_connection.public_path()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    raw_path = request.scope.get("raw_path")
    if raw_path is None:
        raw_path = quote(request.url.path, safe="/").encode("ascii")
    prefix_bytes = prefix.encode("ascii")
    query = request.scope.get("query_string", b"")
    if len(raw_path) + len(query) > 16384:
        raise HTTPException(414, "뷰어 요청 주소가 너무 깁니다.")
    try:
        query_text = query.decode("ascii")
    except UnicodeError:
        raise HTTPException(400, "요청 주소가 올바르지 않습니다.") from None
    if raw_path == prefix_bytes:
        return RedirectResponse(prefix + "/" + ("?" + query_text if query_text else ""), status_code=307)
    if not raw_path.startswith(prefix_bytes + b"/"):
        raise HTTPException(404)
    try:
        target = base + raw_path[len(prefix_bytes):].decode("ascii")
        if query_text:
            target += "?" + query_text
    except UnicodeError:
        raise HTTPException(400, "요청 주소가 올바르지 않습니다.") from None
    length = request.headers.get("content-length")
    if length:
        try:
            size = int(length)
            if size < 0:
                raise ValueError
            if size > MAX_REQUEST_BYTES:
                raise HTTPException(413, "뷰어 요청이 너무 큽니다.")
        except ValueError:
            raise HTTPException(400, "요청 크기가 올바르지 않습니다.") from None
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "뷰어 요청이 너무 큽니다.")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "뷰어 요청을 받는 시간이 초과됐습니다.") from None
    excluded = _HOP_HEADERS | _FORWARDED_HEADERS | _connection_headers(request.headers) | {"host", "content-length"}
    headers = [(name.decode("latin-1"), value.decode("latin-1"))
               for name, value in request.scope["headers"] if name.decode("latin-1").lower() not in excluded]
    headers.extend([("X-Forwarded-Host", request.headers.get("host", "")),
                    ("X-Forwarded-Proto", request.url.scheme), ("X-Forwarded-Prefix", prefix)])
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300, connect=10, sock_read=60),
                                    auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
    try:
        response = await session.request(request.method, URL(target, encoded=True), headers=headers,
                                         data=bytes(body) if body else None, allow_redirects=False)
        if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
            response.close()
            raise HTTPException(502, "뷰어 응답이 너무 큽니다.")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await session.close()
        raise HTTPException(502, "뷰어에 연결하지 못했습니다.") from None
    except BaseException:
        await session.close()
        raise

    async def stream():
        size = 0
        try:
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    log.warning("Viewer response exceeded the streaming size limit")
                    raise RuntimeError("Viewer response exceeded the streaming size limit")
                yield chunk
        finally:
            response.close()
            await session.close()

    result = ViewerResponse(stream(), upstream=response, session=session)
    excluded = _HOP_HEADERS | _connection_headers(response.headers)
    result.raw_headers = []
    for name, value in response.raw_headers:
        key = name.decode("latin-1").lower()
        if key in excluded:
            continue
        if key == "location":
            value = _location(value.decode("latin-1"), base, prefix).encode("latin-1")
        result.raw_headers.append((name.lower(), value))
    return result


def install(app) -> None:
    prefix = viewer_connection.public_path()
    app.add_api_route(prefix, forward, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    app.add_api_route(prefix + "/{path:path}", forward,
                      methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
