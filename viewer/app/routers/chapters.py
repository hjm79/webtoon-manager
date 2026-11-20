"""
zip 내부 이미지(페이지 수/개별 페이지 서빙) + 회차 겹침(리캡) 감지 라우트.

회차 ID는 시리즈 ID와 달리 URL에 시리즈 정보가 안 드러나므로(그냥 불투명한 해시), 공유
프로필이 허용 안 된 시리즈의 회차 ID를 알아내서 직접 찔러보는 걸 막으려면 여기서도
반드시 시리즈 접근권한을 확인해야 한다 - series.py만 막으면 이 라우트로 그대로 우회될 수
있었다.
"""

import asyncio
import logging
import weakref
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import access_control, catalog, db, overlap, page_geometry, scan, services, warmup

log = logging.getLogger("webtoon-server")
router = APIRouter()

# 캐시에 없는 겹침 계산(이미지 약 25장 디코딩 + 템플릿 매칭)은 무거워서, 여러 개가 동시에 돌면 네트워크
# 드라이브용 스레드풀을 전부 차지해 사용자의 페이지 읽기가 줄을 서게 된다. 한 번에 하나씩만 돌린다.
# (이벤트 루프마다 따로 만든다 - 테스트처럼 루프가 여러 개 생겨도 안전하게)
_overlap_limiters: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _overlap_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _overlap_limiters.get(loop)
    if limiter is None:
        limiter = _overlap_limiters[loop] = asyncio.Semaphore(1)
    return limiter


def _resolve_and_authorize(chapter_id: str, profile: dict | None):
    """이 회차가 속한 시리즈를 찾고, 프로필 접근권한 + 접속 가능 시간대까지 확인한다.
    문제 있으면 예외를 던진다."""
    series, index = catalog.find_chapter_position(chapter_id)
    if series is None or index is None:
        raise HTTPException(404, "chapter not found")
    access_control.ensure_series_accessible(profile, series["id"])
    access_control.ensure_reading_time_allowed(profile, series["id"], chapter_id)
    return series["chapters"][index]["path"], series, index


def _page_manifest(zip_path: str, dimensions: bool, include_generation: bool) -> dict:
    for _ in range(2):
        zip_generation = services.chapter_zip_generation(zip_path) if include_generation else None
        names = scan.list_zip_image_names(zip_path)
        result = {"page_count": len(names)}
        if dimensions:
            result["page_sizes"] = page_geometry.chapter_page_sizes(zip_path)
        if zip_generation is None:
            return result
        if zip_generation == services.chapter_zip_generation(zip_path):
            return {**result, "zip_generation": zip_generation}
    raise HTTPException(409, "chapter file changed; reload the chapter")


@router.get("/api/chapters/{chapter_id}/pages")
async def chapter_pages(chapter_id: str, request: Request, dimensions: bool = False, start_reading: bool = False):
    profile = access_control.get_profile(request)
    zip_path, series, index = _resolve_and_authorize(chapter_id, profile)
    generation = None
    if start_reading and profile and access_control.ensure_reading_time_allowed(profile, series["id"], chapter_id):
        generation = db.register_progress_intent(access_control.progress_actor_key(profile), series["id"])
    platform = series["platform"]
    result = await services.run_platform_io(platform, _page_manifest, zip_path, dimensions, start_reading)
    if generation is not None and result["page_count"]:
        access_control.ensure_series_accessible(profile, series["id"])
        db.grant_profile_reading_session(access_control.progress_actor_key(profile), series["id"], generation,
                                         profile["id"], chapter_id)
    if start_reading:
        result["reading_coverage"] = await asyncio.to_thread(db.get_reading_coverage,
            access_control.progress_actor_key(profile), series["id"], chapter_id, result["zip_generation"])
    # 지금 회차를 여는 김에 다음 회차 zip도 서버가 먼저 읽어 둔다(응답을 기다리게 하지 않는 백그라운드 작업)
    warmup.schedule_next_chapter_warmup(series, index)
    return result


@router.get("/api/chapters/{chapter_id}/overlap")
async def chapter_overlap(chapter_id: str, request: Request):
    """
    이 회차 맨 앞부분이 바로 이전 회차(같은 시리즈, 정렬상 직전) 끝부분과 겹치는
    페이지 수를 반환. 결과는 DB에 캐싱되어 다음부터는 즉시 응답한다.
    """
    profile = access_control.get_profile(request)
    zip_path, series, index = _resolve_and_authorize(chapter_id, profile)

    prev_chapter = None
    if index > 0:
        prev_chapter = series["chapters"][index - 1]

    if not prev_chapter:
        return {"skip_pages": 0}

    cached = db.get_cached_overlap(chapter_id)
    if cached is not None:
        return {"skip_pages": cached}

    # 이미지 두 장의 zip을 열어 비교하는 무거운 작업 - 네트워크 드라이브면 특히 오래 걸릴
    # 수 있어 격리된 스레드풀로 넘긴다.
    platform = series["platform"]
    async with _overlap_limiter():
        cached = db.get_cached_overlap(chapter_id)  # 줄 서서 기다리는 동안 다른 요청이 이미 계산해 뒀을 수 있다
        if cached is not None:
            return {"skip_pages": cached}
        skip_pages = await services.run_platform_io(platform, overlap.compute_overlap_pages, prev_chapter["path"], zip_path)
        db.set_cached_overlap(chapter_id, prev_chapter["id"], skip_pages)
    if skip_pages > 0:
        log.info(f"회차 겹침 감지: {chapter_id} 앞부분 {skip_pages}페이지가 이전 화와 중복 (자동 건너뜀)")
    return {"skip_pages": skip_pages}


@router.get("/api/chapters/{chapter_id}/pages/{page_index}")
async def chapter_page(chapter_id: str, page_index: int, request: Request):
    profile = access_control.get_profile(request)
    zip_path, series, _ = _resolve_and_authorize(chapter_id, profile)
    # 실제로 이미지 데이터를 읽는 부분 - 리더가 스크롤하면서 계속 호출하는 가장 빈번한
    # 요청이다. 네트워크 드라이브의 회차를 읽을 때 이게 막히면 서비스 전체 체감 지연이
    # 제일 커서, 반드시 격리된 전용 스레드풀(run_platform_io)로 보내 로컬 회차 열람에는
    # 절대 영향이 없게 한다.
    platform = series["platform"]
    try:
        result = await services.run_platform_io(platform, services.chapter_page_for_http, zip_path, page_index,
                                                request.headers.get("if-none-match", ""))
    except (FileNotFoundError, zipfile.BadZipFile):
        raise HTTPException(404, "page not found") from None
    if result is None:
        raise HTTPException(404, "page not found")
    etag, content = result
    if content is None:
        return Response(status_code=304, headers={"ETag": etag})
    data, media_type = content
    return Response(content=data, media_type=media_type, headers={"ETag": etag})
