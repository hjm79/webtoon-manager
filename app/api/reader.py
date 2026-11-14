"""Report reader availability without exposing integration credentials."""
import asyncio
import json
import logging
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Query

from app import reader_files, repository, viewer_connection
from app.config import get_settings
from app.file_utils import remove_forbidden_str, remove_forbidden_str_kakao

router = APIRouter()
log = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _result(status: str, message: str, url: str | None = None) -> dict:
    return {"status": status, "message": message, "url": url}


def _viewer_base(raw: str) -> str | None:
    try:
        parts = urlsplit(raw.strip())
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(ord(c) < 33 for c in raw)):
            return None
        parts.port
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


async def _lookup(session, base: str, params: dict) -> tuple[int, dict | None]:
    async with session.get(base + "/api/lookup/latest", params=params, allow_redirects=False) as response:
        if response.status != 200:
            return response.status, None
        body = bytearray()
        while True:
            chunk = await response.content.read(min(8192, 65537 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("Reader response exceeds size limit")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Reader response must be an object")
        return response.status, data


@router.get("/reader/status")
async def reader_status(title: str = Query(min_length=1, max_length=300), platform: str = "naver"):
    if platform not in ("naver", "kakao"):
        raise HTTPException(422, "지원하지 않는 플랫폼입니다.")
    if not title.strip():
        raise HTTPException(422, "작품 제목을 입력해주세요.")
    configured = await asyncio.to_thread(repository.get_setting, "webtoon_server_url")
    if not configured and not viewer_connection.integrated():
        return _result("not_configured", "설정에서 웹툰 뷰어 주소를 연결해주세요.")
    try:
        base = _viewer_base(viewer_connection.internal_base(configured))
        public = viewer_connection.public_base(configured)
    except ValueError:
        base = None
    if base is None:
        return _result("error", "뷰어 주소 형식을 확인해주세요. 로그인 정보가 포함되지 않은 HTTP 주소가 필요합니다.")
    safe_title = (remove_forbidden_str_kakao if platform == "kakao" else remove_forbidden_str)(title.strip())
    timeout = aiohttp.ClientTimeout(total=get_settings().request_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, data = await _lookup(session, base, {"series": safe_title, "platform": platform})
            if status == 404:
                status, data = await _lookup(session, base, {"series": safe_title})
            if status == 404:
                count = await asyncio.to_thread(reader_files.completed_count, get_settings(), title, platform)
                if count is None:
                    return _result("error", "로컬 다운로드 폴더를 확인하지 못했습니다. 저장소 연결과 권한을 확인해주세요.")
                if count > 0:
                    return {**_result("indexing", f"받은 회차 {count}개를 서재에 반영하고 있습니다. 잠시 후 다시 눌러주세요."),
                            "downloaded_count": count}
                return {**_result("not_downloaded", "아직 받은 회차가 없습니다. 다운로드 후 읽을 수 있습니다."), "downloaded_count": 0}
            if status in (401, 403):
                return _result("error", "뷰어 인증이 필요합니다. 뷰어에 로그인한 뒤 다시 시도해주세요.")
            if status == 409:
                return _result("error", "같은 제목의 작품이 여러 라이브러리에 있습니다. 뷰어에서 작품을 선택해주세요.")
            if status != 200:
                return _result("error", "뷰어에 연결하지 못했습니다. 주소와 실행 상태를 확인해주세요.")
            series = data.get("series_id")
            if not isinstance(series, str) or not _ID.fullmatch(series):
                return _result("error", "뷰어 응답에 올바른 회차 정보가 없습니다.")
            url = public + "/reader.html?" + urlencode({"series": series})
            return _result("ready", "관리기 안에서 바로 읽을 수 있습니다.", url)
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, ValueError):
        log.warning("Reader availability lookup failed (platform=%s)", platform)
        return _result("error", "뷰어에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.")


@router.get("/viewer/config")
async def viewer_config():
    if not viewer_connection.integrated():
        return {"enabled": False, "public_path": None, "library_url": None, "reader_url": None}
    try:
        viewer_connection.internal_base()
        path = viewer_connection.public_path()
    except ValueError:
        raise HTTPException(503, "통합 뷰어 설정을 확인해주세요.") from None
    return {"enabled": True, "public_path": path, "library_url": path + "/", "reader_url": path + "/reader.html"}
