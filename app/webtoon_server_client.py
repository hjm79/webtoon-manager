"""
별도 프로젝트인 webtoon-server(뷰어)의 /api/lookup/latest API를 호출해서, 방금 받은
작품의 "바로가기" 링크를 얻는다. 계약은 예전 hermes webtoon_checker.py가 쓰던 것과
동일하다고 가정한다: GET {base_url}/api/lookup/latest?series={작품명} -> {"url": "..."}

webtoon-server 자체는 이 프로젝트 소스에 없으므로, 실제 스펙이 다르면 이 파일만
고치면 된다 — 호출부(scheduler.py)는 이 함수의 반환값(성공 시 URL, 실패 시 None)만 본다.
"""

import logging
import re
from urllib.parse import urlencode

import aiohttp

from app import viewer_connection

log = logging.getLogger(__name__)
_PROVIDER_WORK_ID = re.compile(r"[0-9]{1,32}\Z")


async def _fetch_latest(session, lookup_url: str, params: dict, timeout: aiohttp.ClientTimeout) -> tuple[int, dict | None]:
    async with session.get(lookup_url, params=params, timeout=timeout, allow_redirects=False) as response:
        if response.status != 200:
            return response.status, None
        data = await response.json()
        return response.status, data if isinstance(data, dict) else None


async def fetch_reader_url(
    session: aiohttp.ClientSession, base_url: str, series_name: str, timeout_seconds: int, *,
    platform: str | None = None, title_id: str | None = None,
) -> str | None:
    """조회에 실패해도(404, 타임아웃, 형식 오류 등) 예외를 던지지 않고 None을 반환한다 —
    이 링크는 리포트 메시지를 보강하는 부가 정보일 뿐이라, 실패해도 리포트 자체가
    중단되면 안 된다."""
    try:
        base_url = viewer_connection.internal_base(base_url)
        if not base_url:
            return None
        url = base_url.rstrip("/") + "/api/lookup/latest"
        params = {"series": series_name}
        if platform:
            params["platform"] = platform
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        status, data = await _fetch_latest(session, url, params, timeout)
        if status == 404 and platform in {"naver", "kakao"} and title_id and _PROVIDER_WORK_ID.fullmatch(title_id):
            _, data = await _fetch_latest(session, url, {
                "series": series_name, "platform": "archive",
                "origin_platform": platform, "origin_title_id": title_id,
            }, timeout)
            # A legacy viewer can ignore the additional query keys.
            if not data or data.get("origin_platform") != platform or data.get("origin_title_id") != title_id:
                return None
        if not data:
            return None
        if viewer_connection.integrated():
            return viewer_connection.report_reader_url(data.get("url"), data.get("series_id"), data.get("chapter_id"))
        return data.get("url") or None
    except Exception as e:
        log.warning("webtoon-server 바로가기 조회 실패 (series=%s): %s", series_name, e)
        return None


async def fetch_read_numbers(
    session: aiohttp.ClientSession, base_url: str, series_name: str, timeout_seconds: int, *, platform: str | None = None
) -> frozenset[int] | None:
    """뷰어에서 이 작품의 '읽은' 회차 번호들을 얻는다: GET {base_url}/api/lookup/read-state?series={작품명}
    -> {"series_id": "...", "read_numbers": [1, 2, ...]} ('읽는 중'인 회차는 뷰어가 이미 뺀다).
    실패(미설정, 404, 타임아웃, 형식 오류)는 None — 호출부가 읽음 상태를 알 수 없는 작품을 건너뛴다.
    읽은 회차가 하나도 없는 것(빈 집합)과 구분된다."""
    try:
        base_url = viewer_connection.internal_base(base_url)
        if not base_url:
            return None
        url = base_url.rstrip("/") + "/api/lookup/read-state"
        params = {"series": series_name}
        if platform:
            params["platform"] = platform
        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
            return frozenset(int(n) for n in data["read_numbers"])
    except Exception as e:
        log.warning("webtoon-server 읽음 상태 조회 실패 (series=%s): %s", series_name, e)
        return None


_SCAN_HEADER = "X-Integration-Token"


async def start_scan(session: aiohttp.ClientSession, base_url: str, token: str, timeout_seconds: int,
                     *, request_id: str | None = None) -> str:
    """뷰어에 스캔을 시킨다: POST {base_url}/api/integration/rescan (헤더 X-Integration-Token). 스캔이 오래 걸려도 응답은 바로 온다.
    "started"(요청 수락) / "running"(다른 스캔 중) / "legacy"(ID 미지원) / "unsupported"(404/401/403) / "error"(연결 오류 등)."""
    try:
        base_url = viewer_connection.internal_base(base_url)
        headers = {_SCAN_HEADER: token}
        if request_id is not None:
            headers["X-Scan-Request-Id"] = request_id
        async with session.post(
            base_url.rstrip("/") + "/api/integration/rescan", headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as response:
            if response.status in (401, 403, 404):
                return "unsupported"
            if response.status != 200:
                log.warning("뷰어 스캔 시작 실패: HTTP %s", response.status)
                return "error"
            data = await response.json(content_type=None)
            if not isinstance(data, dict) or not isinstance(data.get("started"), bool):
                return "error"
            if request_id is not None:
                if data.get("scan_id") is None:
                    return "legacy"
                if data["scan_id"] != request_id:
                    return "error"
            return "started" if data["started"] else "running"
    except Exception as e:
        log.warning("뷰어 스캔 시작 중 오류: %s", e)
        return "error"


async def scan_status(session: aiohttp.ClientSession, base_url: str, token: str, timeout_seconds: int,
                      *, scan_id: str | None = None) -> dict | None:
    """뷰어의 스캔 진행 상태: GET {base_url}/api/integration/scan-status → {"running": bool, ...}. 못 읽으면 None(다음에 다시 확인)."""
    try:
        base_url = viewer_connection.internal_base(base_url)
        url = base_url.rstrip("/") + "/api/integration/scan-status"
        if scan_id is not None:
            url += "?" + urlencode({"scan_id": scan_id})
        async with session.get(
            url, headers={_SCAN_HEADER: token}, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
            return data if isinstance(data, dict) and isinstance(data.get("running"), bool) else None
    except Exception as e:
        log.warning("뷰어 스캔 상태 확인 중 오류: %s", e)
        return None

