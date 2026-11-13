"""Unified discovery and explicit first-subscription download workflow."""
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Literal

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app import daily_plus, download_roots, job_status, kakao_api, kakao_catalog, kakao_page_download
from app import manual_download, naver_api, repository, subscription_scope, workflow_progress
from app.api import kakao_webtoons, webtoons
from app.config import get_settings
from app.cookie_loader import get_adult_cookies
from app.file_utils import remove_forbidden_str, remove_forbidden_str_kakao
from app.folder_scanner import find_downloaded_episode_numbers
from app.kakao_filters import is_excluded_title
from app.scheduler import download_job

router = APIRouter(prefix="/workflow")
log = logging.getLogger(__name__)
_subscription_lock = asyncio.Lock()
Platform = Literal["naver", "kakao"]


class TitleIn(BaseModel):
    platform: Platform
    title_id: str

    @field_validator("title_id", mode="before")
    @classmethod
    def valid_id(cls, value):
        text = str(value).strip()
        if not text.isascii() or not text.isdigit() or int(text) <= 0 or len(text) > 15:
            raise ValueError("작품 번호는 양의 정수여야 합니다.")
        return str(int(text))


class ScopeIn(BaseModel):
    mode: Literal["all", "latest", "selected", "future"]
    latest_count: int | None = Field(default=None, ge=1, le=1000)
    episode_nos: list[int] = Field(default_factory=list, max_length=10000)

    @field_validator("episode_nos")
    @classmethod
    def valid_numbers(cls, value):
        if any(n <= 0 for n in value):
            raise ValueError("회차 번호는 양의 정수여야 합니다.")
        return sorted(set(value))


class SubscribeIn(TitleIn):
    title: str = Field(min_length=1, max_length=300)
    thumbnail_url: str = Field(default="", max_length=2000)
    author_summary: str = Field(default="", max_length=1000)
    scope: ScopeIn
    start_now: bool = False


def _matches(query: str, *values) -> bool:
    needle = "".join(query.casefold().split())
    return any(needle in "".join(str(v).casefold().split()) for v in values if v)


async def _search_naver(query: str) -> list[dict]:
    settings = get_settings()
    async with aiohttp.ClientSession() as session:
        items = await naver_api.search_webtoons(session, query, settings.request_timeout_seconds)
        hidden = await daily_plus.current_ids(settings.request_timeout_seconds, session)
    tracked = {w.title_id: w for w in await asyncio.to_thread(repository.list_all)}
    return [{"platform": "naver", "title_id": str(item.title_id), "title": item.title_name,
             "thumbnail_url": item.thumbnail_url, "subscription": tracked[item.title_id].status if item.title_id in tracked else None,
             "is_paused": item.is_paused, "is_finished": item.is_finished}
            for item in items if item.title_id not in hidden]


async def _search_kakao(query: str) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        items = await kakao_api.search_series(session, query, get_settings().request_timeout_seconds)
    tracked = await asyncio.to_thread(repository.get_kakao_webtoons_map)
    return [{"platform": "kakao", "title_id": str(item["title_id"]), "title": item["title_name"],
             "thumbnail_url": item.get("thumbnail_url", ""), "subscription": (tracked.get(item["title_id"]) or {}).get("status"),
             "is_paused": item.get("is_paused", False), "is_finished": item.get("is_finished", False),
             "author_summary": ", ".join(item.get("author_names", []))}
            for item in items if not is_excluded_title(item["title_name"])]


@router.get("/search")
async def search(query: str = Query(min_length=1, max_length=100), platform: Literal["all", "naver", "kakao"] = "all"):
    query = query.strip()
    if not query:
        raise HTTPException(400, "검색할 제목이나 작가를 입력해주세요.")
    results = {}
    hidden = daily_plus.cached_ids()
    if platform in ("all", "naver"):
        for row in await asyncio.to_thread(repository.list_all):
            if row.title_id not in hidden and _matches(query, row.title, *row.writer_names):
                results[("naver", row.title_id)] = {"platform": "naver", "title_id": row.title_id, "title": row.title,
                    "thumbnail_url": row.thumbnail_url, "subscription": row.status,
                    "is_paused": row.is_paused, "is_finished": row.is_finished}
    if platform in ("all", "kakao"):
        tracked = await asyncio.to_thread(repository.get_kakao_webtoons_map)
        for row in tracked.values():
            if not is_excluded_title(row["title"]) and _matches(query, row["title"], row.get("author_summary")):
                results[("kakao", str(row["title_id"]))] = {"platform": "kakao", "title_id": str(row["title_id"]),
                    "title": row["title"], "thumbnail_url": row.get("thumbnail_url", ""), "subscription": row["status"],
                    "author_summary": row.get("author_summary", ""), "is_finished": bool(row.get("is_finished"))}
        for row in kakao_catalog.snapshot()[0]:
            if not is_excluded_title(row["title_name"]) and _matches(query, row["title_name"], *row.get("author_names", [])):
                key = ("kakao", str(row["title_id"]))
                results.setdefault(key, {"platform": "kakao", "title_id": key[1], "title": row["title_name"],
                    "thumbnail_url": row.get("thumbnail_url", ""), "subscription": None,
                    "is_paused": row.get("is_paused", False), "is_finished": row.get("is_finished", False)})
    platforms = ["naver", "kakao"] if platform == "all" else [platform]
    responses = await asyncio.gather(*[(_search_naver if name == "naver" else _search_kakao)(query) for name in platforms],
                                     return_exceptions=True)
    errors = []
    for name, response in zip(platforms, responses):
        if isinstance(response, BaseException):
            log.warning("통합 검색의 %s 응답을 받지 못했습니다 (%s)", name, type(response).__name__)
            errors.append({"platform": name, "message": "사이트 검색을 불러오지 못했습니다. 저장된 결과를 표시합니다."})
        else:
            for row in response:
                results[(name, row["title_id"])] = row
    current_hidden = daily_plus.cached_ids()
    results = {key: row for key, row in results.items() if key[0] != "naver" or key[1] not in current_hidden}
    return {"results": sorted(results.values(), key=lambda r: (r["title"] != query, r["platform"], r["title"]))[:60], "errors": errors}


async def _preview(platform: str, title_id: str) -> dict:
    settings = get_settings()
    if platform == "naver":
        if title_id in await daily_plus.current_ids(settings.request_timeout_seconds):
            raise HTTPException(400, "매일+ 작품은 이 앱의 검색·구독 대상에서 제외됩니다.")
        tracked = await asyncio.to_thread(repository.get, title_id)
        info, rows = await manual_download.analyze(title_id, settings, tracked.last_downloaded_no if tracked else 0)
        if info is None:
            raise HTTPException(404, "네이버에서 작품을 찾지 못했습니다.")
        authenticated = not info.is_adult or bool(get_adult_cookies(settings.cookie_file_path))
        owned = repository.downloaded_episode_numbers(title_id)
        folder = Path(download_roots.naver_root(settings)) / remove_forbidden_str(info.title_name)
        owned |= await asyncio.to_thread(find_downloaded_episode_numbers, folder, info.title_name, rows)
        episodes = [{"episode_no": row.episode_no, "subtitle": row.subtitle,
                     "available": not row.is_locked and authenticated, "owned": row.episode_no in owned} for row in rows]
        title = info.title_name
    else:
        async with kakao_page_download.new_session() as session:
            client = kakao_page_download.client_or_anonymous(session, settings.request_timeout_seconds)
            listing = await client.list_episodes(int(title_id))
            kakao_page_download.persist_refreshed_cookies(client)
        if listing is None:
            raise HTTPException(502, "카카오페이지 회차 목록을 불러오지 못했습니다.")
        item, rows = listing
        title = item.get("title") or title_id
        if is_excluded_title(title):
            raise HTTPException(400, "단행본은 구독 대상에서 제외됩니다.")
        folder = kakao_page_download.series_folder(download_roots.kakao_root(settings), title)
        existing = await asyncio.to_thread(kakao_page_download.scan_existing_files, folder)
        owned = {f.number for f in existing} | (repository.get_kakao_downloaded_numbers(int(title_id)) or set())
        episodes = [{"episode_no": row.number, "subtitle": row.subtitle,
                     "available": row.accessible and not row.hidden, "owned": row.number in owned} for row in rows]
    return {"platform": platform, "title_id": title_id, "title": title, "episodes": episodes,
            "available_count": sum(row["available"] for row in episodes)}


@router.post("/preview")
async def preview(payload: TitleIn):
    try:
        return await _preview(payload.platform, payload.title_id)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("구독 범위 조회 실패 (%s)", type(exc).__name__)
        raise HTTPException(502, "회차 목록을 불러오지 못했습니다. 잠시 뒤 다시 시도해주세요.") from exc


def next_run_at(request: Request, platform: str | None = None) -> str | None:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return None
    times = []
    for job in scheduler.get_jobs():
        if not job.id.startswith("download_job") or getattr(job, "next_run_time", None) is None:
            continue
        target = getattr(job, "kwargs", {}).get("target", "naver")
        if platform is None or target in (platform, "both"):
            times.append(job.next_run_time)
    return min(times).astimezone(timezone.utc).isoformat() if times else None


async def _require_kakao_login() -> None:
    async with kakao_page_download.new_session() as session:
        client = kakao_page_download.client_from_saved_cookies(session, get_settings().request_timeout_seconds)
        if client is None:
            raise HTTPException(400, "카카오페이지 로그인 쿠키가 없습니다. 설정에서 쿠키를 먼저 저장해주세요.")
        try:
            logged_in = await client.check_login()
        except Exception as exc:
            raise HTTPException(502, "카카오페이지 로그인 상태를 확인하지 못했습니다.") from exc
        kakao_page_download.persist_refreshed_cookies(client)
        if logged_in is False:
            raise HTTPException(401, "카카오페이지 로그인이 만료되었습니다. 설정에서 쿠키를 다시 등록해주세요.")
        if logged_in is not True:
            raise HTTPException(502, "카카오페이지 로그인 상태를 확인하지 못했습니다.")


@router.post("/subscribe")
async def subscribe(payload: SubscribeIn, request: Request):
    async with _subscription_lock:
        if download_job.download_busy() or kakao_page_download.download_lock.locked():
            raise HTTPException(409, "다른 다운로드가 실행 중입니다. 완료 후 다시 시도해주세요.")
        old = repository.get(payload.title_id) if payload.platform == "naver" else repository.get_kakao_webtoon(int(payload.title_id))
        if old is not None and (old.status if payload.platform == "naver" else old["status"]) == repository.STATUS_ACTIVE:
            raise HTTPException(409, "이미 구독 중인 작품입니다. 기존 다운로드 범위는 유지합니다.")
        listing = await preview(TitleIn(platform=payload.platform, title_id=payload.title_id))
        if payload.start_now and payload.platform == "kakao":
            await _require_kakao_login()
        reserved = False
        try:
            if payload.start_now:
                download_job.reserve_title_download(payload.platform)
                reserved = True
            elif download_job.download_busy():
                raise HTTPException(409, "다른 다운로드가 시작되었습니다. 완료 후 다시 시도해주세요.")
            scope = await asyncio.to_thread(subscription_scope.set_choice, payload.platform, payload.title_id,
                payload.scope.mode, listing["episodes"], latest_count=payload.scope.latest_count,
                episode_nos=payload.scope.episode_nos)
            if payload.platform == "naver":
                entry = webtoons.NaverListEntryIn(title=listing["title"], thumbnail_url=payload.thumbnail_url)
                subscribed = await webtoons.naver_list_subscribe(payload.title_id, entry)
            else:
                entry = kakao_webtoons.KakaoWebtoonEntryIn(title=listing["title"], thumbnail_url=payload.thumbnail_url,
                                                          author_summary=payload.author_summary)
                subscribed = await kakao_webtoons.subscribe_kakao_webtoon(int(payload.title_id), entry)
            if payload.start_now:
                download_job.enqueue_reserved_title(payload.platform, payload.title_id, listing["title"])
                reserved = False
            return {"subscription": subscribed, "scope": scope,
                    "execution": "queued" if payload.start_now else "scheduled", "next_run_at": next_run_at(request, payload.platform)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if reserved:
                download_job.release_title_download(payload.platform)


def _file_count(root: str, title: str, platform: str) -> int:
    safe = remove_forbidden_str(title) if platform == "naver" else remove_forbidden_str_kakao(title)
    if safe in ("", ".", ".."):
        return 0
    folder = Path(root) / safe
    if not folder.is_relative_to(Path(root)):
        return 0
    try:
        return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in (".zip", ".cbz"))
    except OSError:
        return 0


def _subscriptions() -> list[dict]:
    settings = get_settings()
    result = []
    for platform, rows in (("naver", repository.list_by_status(repository.STATUS_ACTIVE)),
                            ("kakao", repository.list_kakao_webtoons_by_status(repository.STATUS_ACTIVE))):
        root = download_roots.naver_root(settings) if platform == "naver" else download_roots.kakao_root(settings)
        for row in rows:
            title_id, title = (row.title_id, row.title) if platform == "naver" else (str(row["title_id"]), row["title"])
            progress = workflow_progress.title_snapshot(platform, title_id)
            result.append({"platform": platform, "title_id": title_id, "title": title,
                           "scope": subscription_scope.get(platform, title_id),
                           "downloaded_count": _file_count(root, title, platform),
                           "last_downloaded_no": row.last_downloaded_no if platform == "naver" else None,
                           "status": progress["state"] if progress else "idle", "progress": progress})
    return result


@router.get("/status")
async def status(request: Request):
    subscriptions = await asyncio.to_thread(_subscriptions)
    for row in subscriptions:
        row["next_run_at"] = next_run_at(request, row["platform"])
        if row["progress"] is None:
            if row["downloaded_count"] > 0 and not row["scope"]["pending_initial"]:
                row["status"] = "available"
            else:
                row["status"] = "scheduled" if row["next_run_at"] else "idle"
    return {"next_run_at": next_run_at(request), "jobs": job_status.snapshot(), "progress": workflow_progress.snapshot(),
            "subscriptions": subscriptions, "checked_at": datetime.now(timezone.utc).isoformat()}
