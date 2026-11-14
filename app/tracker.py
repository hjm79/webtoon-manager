"""
구독 중인 웹툰을 스캔해서
  1) 완결/휴재 여부, 장르·태그를 갱신하고
  2) "등록된 작가"(watched_authors, enabled=1)의 다른 신작을 자동 구독에 추가하고
  3) "등록된 태그"(watched_tags, enabled=1)에 새로 편입된 작품을 자동 구독에 추가한다.

작가/태그는 구독 여부와 분리된 별도 레지스트리(watched_authors/watched_tags)로 관리한다.
작가는 자동 등록 설정을 따르며, 이미 직접 선택한 켜짐/꺼짐은 유지한다.
작가/태그로 새 구독을 추가하는 기능은 별도 설정에서 명시적으로 켠 경우에만 동작한다.

웹툰 하나 처리 중 예외가 나도 전체 스캔은 계속되도록 개별 웹툰 단위로 예외를 격리한다.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import aiohttp

from app import auto_subscribe, kakao_catalog, kakao_status, weekdays, daily_plus, download_roots, comicinfo, discord_notify, job_status, kakao_api, kakao_cover, kakao_page_download, naver_api, repository
from app.file_utils import remove_forbidden_str, remove_forbidden_str_kakao
from app.config import Settings
from app.discord_notify import send_webhook_notification
from app.models import TitleInfo
from app import kakao_authors, subscription_history, subscription_scope

log = logging.getLogger(__name__)

DEFAULT_TAG_IDS_AND_NAMES = {"134": "", "133": ""}


def ensure_default_tags_seeded() -> None:
    """watched_tags 테이블이 비어있으면(최초 실행) 기본 태그를 채운다 — 단, 꺼진
    상태(enabled=False)로만 채운다. 예전엔 켜진 상태로 심어서 사용자가 요청한 적
    없는 태그가 자동으로 활성화되어 있었고, 그 결과 원치 않는 작품이 자동구독
    돼버리는 문제가 실제로 있었다 — 사용자가 "작가/태그 관리"에서 원하면 직접
    켜도록, 기본은 항상 꺼진 채로 등록만 해둔다."""
    if repository.list_watched_tags():
        return
    for tag_id in DEFAULT_TAG_IDS_AND_NAMES:
        repository.upsert_watched_tag(tag_id, "", enabled=False)


async def _fetch_info_and_others(
    session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, title_id: str, settings: Settings
) -> tuple[str, TitleInfo | None, list[dict]]:
    async with semaphore:
        try:
            info = await naver_api.fetch_title_info(session, title_id, settings.request_timeout_seconds)
            others: list[dict] = []
            await asyncio.sleep(settings.delay_seconds)
            return title_id, info, others
        except Exception as e:
            log.error("웹툰(titleId=%s) 스캔 중 예외 — 이 항목만 건너뜁니다: %s", title_id, e)
            return title_id, None, []


def authors_to_register(info: TitleInfo) -> list[tuple[str, str]]:
    """관심 작가로 자동 등록할 (id, 이름) — 원작자가 있으면 원작자를, 없으면 작가를 등록한다(작가와 원작자가 둘 다
    있는 작품은 원작자만)."""
    return list(info.origin_id_name_pairs) or list(info.writer_id_name_pairs)


def _store_origin_authors(title_id: str, info: TitleInfo) -> None:
    ids = [author_id for author_id, _ in info.origin_id_name_pairs]
    names = [name for _, name in info.origin_id_name_pairs]
    repository.update_origin_ids_and_names(title_id, ids, names)


def _matches_enabled_writer(other: dict, enabled_author_ids: set[str]) -> bool:
    """다른 작품의 작가 목록에 등록된 작가가 있는지. 작가(writers)뿐 아니라 응답의 author 안에 있는 모든 사람 목록에서
    id를 찾는다 — 원작자가 어느 필드로 오는지 확인된 응답 예시가 없어서, 필드 이름에 기대지 않는다."""
    author = other.get("author") or {}
    other_ids = {
        str(person.get("id"))
        for people in author.values() if isinstance(people, list)
        for person in people if isinstance(person, dict) and person.get("id") is not None
    }
    return bool(enabled_author_ids & other_ids)


async def _is_other_finished(
    session: aiohttp.ClientSession, other: dict, other_title_id: str, settings: Settings
) -> bool:
    for key in ("finished", "finish"):
        if key in other:
            return bool(other[key])
    info = await naver_api.fetch_title_info(session, other_title_id, settings.request_timeout_seconds)
    return bool(info and info.is_finished)


async def search_authors_by_name(
    session: aiohttp.ClientSession, name: str, settings: Settings
) -> list[dict]:
    """네이버 통합검색으로 작가 이름을 찾는다 (검색 결과에 작가 id/이름이 직접 들어있음)."""
    results = await naver_api.search_webtoons(session, name, settings.request_timeout_seconds)

    found: dict[str, dict] = {}
    for item in results:
        for author_id, author_name in item.author_ids_names:
            if name in author_name and author_id not in found:
                found[author_id] = {
                    "author_id": author_id,
                    "author_name": author_name,
                    "sample_title": item.title_name,
                    "sample_title_id": item.title_id,
                }
    return list(found.values())


async def backfill_missing_thumbnails(session: aiohttp.ClientSession, settings: Settings) -> int:
    """
    상태(구독중/구독해제/제외됨)와 무관하게, 썸네일 URL이 아직 없는 모든 웹툰의
    썸네일을 채운다. 신작 자동추가 같은 부수효과는 없이 정보 조회만 한다 —
    그래서 목록제외된 웹툰도 여기서는 대상에 포함된다.
    """
    targets = [wt for wt in repository.list_all() if not wt.thumbnail_url]
    if not targets:
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)

    async def _fetch_one(title_id: str) -> tuple[str, TitleInfo | None]:
        async with semaphore:
            try:
                info = await naver_api.fetch_title_info(session, title_id, settings.request_timeout_seconds)
                await asyncio.sleep(settings.delay_seconds)
                return title_id, info
            except Exception as e:
                log.error("썸네일 백필 중 예외 (titleId=%s): %s", title_id, e)
                return title_id, None

    results = await asyncio.gather(*(_fetch_one(wt.title_id) for wt in targets))

    filled = 0
    for title_id, info in results:
        if info is None or not info.thumbnail_url:
            continue
        repository.update_thumbnail_url(title_id, info.thumbnail_url)
        repository.update_is_adult(title_id, info.is_adult)
        repository.update_genres_and_tags(title_id, info.genres_ko, info.tags)
        repository.update_is_paused(title_id, info.is_paused)
        repository.update_is_new(title_id, info.is_new)
        filled += 1

    return filled


async def enrich_one(
    session: aiohttp.ClientSession, title_id: str, settings: Settings, register_authors_enabled: bool = True,
    report: list[str] | None = None, *, preserve_author_selection: bool = False,
) -> tuple[bool, str]:
    """
    구독(또는 목록제외) 직후, 혹은 재동기화 때 실행 — 정보(썸네일/장르/태그/휴재/작가)를
    채우고 작가를 레지스트리에 등록한다. (성공 여부, 사람이 읽을 결과 메시지)를 반환한다 —
    예전엔 정보 조회 실패를 그냥 조용히 넘어가서 "재동기화는 성공했다는데 왜 목록이
    비어있지?"를 진단할 방법이 없었다.

    register_authors_enabled=False로 부르면 작가는 등록하되 "등록된 작가"(자동 신작추가
    대상)로는 올리지 않는다 — 목록제외한 웹툰이나, 아직 구독 안 한 웹툰의 저자를
    "전체 작가 목록" 채우기 용도로 알아낼 때 쓴다.

    report를 넘기면 작가 등록 결과(작가별 신규/이미 등록됨/꺼져 있어 켜지 않음)를 설명하는 줄을 거기에 담는다 — 구독 실행 이력용.
    """
    try:
        info = await naver_api.fetch_title_info(session, title_id, settings.request_timeout_seconds)
    except Exception as e:
        return False, f"정보 조회 예외: {e}"

    if info is None:
        return False, "정보 조회 실패 (네이버 API 응답 없음/오류)"

    repository.update_is_adult(title_id, info.is_adult)
    repository.update_thumbnail_url(title_id, info.thumbnail_url)
    repository.update_genres_and_tags(title_id, info.genres_ko, info.tags)
    repository.update_is_paused(title_id, info.is_paused)
    repository.update_is_new(title_id, info.is_new)
    # 연재 요일(작품 정보 API)도 조회 결과로 저장한다 — 요일별 목록에서 빠진 휴재 작품의 요일을 전체 재동기화/신작 스캔이 한 번에 채운다(목록에 있으면 목록의 요일이 우선).
    if info.weekdays or info.is_finished:  # 요일이 비어 있고 완결도 아니면 저장하지 않는다 — "요일 없음"으로 기록하면 요일 조회(모바일 페이지 폴백)가 일주일 동안 다시 안 묻는다
        weekdays.save_lookup(title_id, info.weekdays, http=200, finished=info.is_finished)
    tracked = repository.get(title_id)
    if tracked is not None and tracked.status != repository.STATUS_ACTIVE:
        # 제외됨/구독해제 작품의 완결 여부 — 예전엔 구독 중인 작품만 저장해서 완결 작품이 제외됨에 그대로 보이고 요일 조회도 완결 작품에 헛수고를 했다.
        # 구독 중인 작품은 구독하는 순간 완결 알림이 나가지 않게 기존처럼 신작 스캔(scan_subscriptions_for_updates)이 맡는다.
        repository.update_is_finished(title_id, info.is_finished)
    if info.writer_id_name_pairs:
        ids, names = zip(*info.writer_id_name_pairs)
        repository.update_writer_ids_and_names(title_id, list(ids), list(names))
    _store_origin_authors(title_id, info)

    results = []
    for author_id, author_name in authors_to_register(info):
        state = repository.upsert_watched_author(author_id, author_name, enabled=register_authors_enabled, preserve_selection=preserve_author_selection)
        results.append((author_name or author_id, state))

    if results:
        role_label = "원작자" if info.origin_id_name_pairs else "작가"
        if report is not None:
            report.append(subscription_history.describe_registration(role_label, results, register_authors_enabled))
        label = f"{role_label} 정보 갱신(선택 유지)" if preserve_author_selection else f"{role_label} 등록"
        return True, f"{label}: {', '.join(name for name, _ in results)}"
    return True, "작가 정보 없음 (API 응답에 작가 필드가 비어있음)"


async def _record_kakao_title_authors(series_id: int, settings) -> None:
    """신작 스캔으로 자동 추가한 카카오 작품의 작가 정보(글/그림/원작 구분)를 작품 정보에서 받아 저장하고, 구독할 때와 같은 규칙(원작자가
    있으면 원작자, 없으면 글 작가)으로 관심 작가를 등록한다(구독 시 작가 자동 등록 설정이 켜져 있을 때). 사용자가 꺼 둔 작가는 다시 켜지
    않는다. 작품 정보를 못 받으면 작가는 건드리지 않고 실행 이력에만 남긴다 — 작품 추가와 알림은 이미 끝났고, 작가 정보는 다음 다운로드/재동기화 때 채워진다."""
    try:
        async with kakao_page_download.new_session() as kakao_session:
            about = await kakao_page_download.client_or_anonymous(kakao_session, settings.request_timeout_seconds).fetch_about(series_id)
    except Exception as e:
        log.warning("카카오 신작 작품 정보(series_id=%s) 조회 실패 — 작가 정보는 나중에 채웁니다: %s", series_id, e)
        about = None
    if about is not None:
        writers, _, originals = kakao_authors.split_authors(about)
        repository.set_kakao_authors(series_id, writers, originals)
    subscription_history.record_kakao_subscription(
        (repository.get_kakao_webtoon(series_id) or {}).get("title") or str(series_id), about,
        repository.is_author_auto_register_enabled(), action="신작 스캔으로 자동 구독",
    )


async def _notify_kakao_auto_add(session: aiohttp.ClientSession, author_name: str, title_text: str, title_id: int) -> None:
    """명시적으로 허용한 카카오 신작 자동 추가를 알린다. 알림 실패는 로그에 남긴다."""
    message = f"🆕 **카카오웹툰 신작 자동 추가**\n작가: {author_name}\n제목: {title_text}\nID: {title_id}"
    try:
        await discord_notify.send_webhook_notification(session, message)
    except Exception as e:
        log.error("카카오 신작 알림 전송 실패: %s", e)


async def _automatic_scope(platform: str, title_id: str, settings: Settings) -> tuple[dict, dict]:
    """Read episode metadata only; no download, ticket use or payment."""
    policy = auto_subscribe.scope_config()
    if platform == "naver":
        from app import manual_download
        from app.cookie_loader import get_adult_cookies
        info, rows = await manual_download.analyze(title_id, settings)
        if info is None:
            raise ValueError("작품 정보 조회 실패")
        authenticated = not info.is_adult or bool(get_adult_cookies(settings.cookie_file_path))
        episodes = [{"episode_no": r.episode_no, "available": not r.is_locked and authenticated, "owned": False} for r in rows]
    else:
        async with kakao_page_download.new_session() as session:
            client = kakao_page_download.client_or_anonymous(session, settings.request_timeout_seconds)
            listing = await client.list_episodes(int(title_id))
            kakao_page_download.persist_refreshed_cookies(client)
        if listing is None:
            raise ValueError("회차 목록 조회 실패")
        episodes = [{"episode_no": r.number, "available": r.accessible and not r.hidden, "owned": False} for r in listing[1]]
    if not episodes:
        raise ValueError("회차 목록이 비어 있어 자동 구독을 보류합니다")
    return policy, subscription_scope.make_choice(platform, title_id, policy["mode"], episodes, latest_count=policy["latest_count"])


async def scan_kakao_authors_for_new_titles(session: aiohttp.ClientSession, settings) -> int:
    """Discover candidates even with auto subscription OFF; baseline each new source."""
    if repository.get_setting("kakao_webtoons_enabled") != "1":
        return 0
    added = 0
    for author in repository.list_watched_authors(platform="kakao"):
        if not author.enabled:
            continue
        try:
            results = await kakao_api.search_by_author(session, author.author_name, settings.request_timeout_seconds)
            if repository.get_setting(f"registry_seen:kakao:artist:{author.author_id}") is None and not repository.get_seen_kakao_title_ids(author.author_id):
                job_status.log_line("discovery", f"[카카오/{author.author_name}] 기존 작품 {len(results)}개를 기준선으로만 저장합니다")
            candidates = auto_subscribe.observe("kakao", repository.SOURCE_ARTIST, author.author_id, author.author_name,
                [{"title_id": str(r["title_id"]), "title": r["title_name"]} for r in results])
            pending = {r["title_id"] for r in candidates}
            for item in results:
                title_id = item["title_id"]
                if repository.get_kakao_webtoon(title_id) is not None:
                    job_status.log_line("discovery", f"[카카오/{author.author_name}] {item['title_name']}: 이미 추적 중이라 건너뜀")
                if str(title_id) not in pending or item.get("is_finished") or not auto_subscribe.is_enabled():
                    continue
                try:
                    policy, scope = await _automatic_scope("kakao", str(title_id), settings)
                except Exception as exc:
                    log.warning("카카오 자동 구독 범위 조회 보류 (%s): %s", title_id, exc)
                    continue
                if not auto_subscribe.add_kakao(title_id, item["title_name"], item.get("thumbnail_url") or "",
                        ", ".join(item.get("author_names") or []), source_id=author.author_id, scope=scope, policy=policy):
                    continue
                added += 1
                job_status.log_line("discovery", f"[카카오/{author.author_name}] 신작 자동 추가: {item['title_name']}")
                await _record_kakao_title_authors(title_id, settings)
                await _notify_kakao_auto_add(session, author.author_name, item["title_name"], title_id)
        except Exception as exc:
            log.error("카카오 작가 검색 오류 (%s): %s", author.author_name, exc)
            job_status.log_line("discovery", f"[카카오/{author.author_name}] 검색 오류: {exc}")
        await asyncio.sleep(settings.delay_seconds)
    return added


async def _fetch_info_only(
    session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, title_id: str, settings: Settings
) -> tuple[str, TitleInfo | None]:
    """메타 동기화 전용 — 다른 스캔들과 달리 '같은 작가의 다른 작품' 조회는 필요
    없어서, 그 부분만 뺀 가벼운 버전."""
    async with semaphore:
        try:
            info = await naver_api.fetch_title_info(session, title_id, settings.request_timeout_seconds)
            await asyncio.sleep(settings.delay_seconds)
            return title_id, info
        except Exception as e:
            log.error("웹툰(titleId=%s) 메타 조회 중 예외 — 이 항목만 건너뜁니다: %s", title_id, e)
            return title_id, None


async def sync_metadata_for_all(settings) -> int:
    """
    추적 중인(구독중/구독해제/제외됨 전부) 웹툰 폴더를 스캔해서 info.xml/cover를
    다시 만들어준다. 폴더가 아예 없으면(다운로드한 적 없는 웹툰) 건너뛴다.

    info.xml과 커버는 있어도 매번 네이버에서 새로 받아와 교체한다 — DB에 저장된 값만으로
    info.xml을 만들면 글작가/그림작가/원작자처럼 DB에 아예 저장 안 하는 정보는 영원히
    못 채우고, 커버도 예전 것이 그대로 굳어 버리기 때문이다("누락분만"이던 예전 방식의
    한계). 다운로드 중에는 커버가 없을 때만 받는 기존 동작 그대로이고, 이 메타 동기화만
    새로 받는다.

    한 웹툰 처리 중 예외가 나도(디스크 오류, 커버 이미지 네트워크 실패 등) 전체가
    멈추지 않고 다음 웹툰으로 넘어가야 하므로, 다른 스캔 함수들과 동일하게 웹툰
    단위로 예외를 격리한다.
    """
    targets = [wt for wt in repository.list_all() if (Path(download_roots.naver_root(settings)) / remove_forbidden_str(wt.title)).is_dir()]
    if not targets:
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)
    fixed = 0
    async with aiohttp.ClientSession() as session:
        fetch_results = await asyncio.gather(
            *[_fetch_info_only(session, semaphore, wt.title_id, settings) for wt in targets]
        )
        info_by_id = {title_id: info for title_id, info in fetch_results}

        for wt in targets:
            try:
                info = info_by_id.get(wt.title_id)
                if info is None:
                    job_status.log_line("metadata_sync", f"[{wt.title}] 네이버 조회 실패로 건너뜀")
                    continue

                webtoon_dir = Path(download_roots.naver_root(settings)) / remove_forbidden_str(wt.title)
                repository.update_genres_and_tags(wt.title_id, info.genres_ko, info.tags)

                comicinfo.write_comicinfo_file(webtoon_dir, info)
                if info.thumbnail_url:
                    # 메타 동기화는 커버도 새로 받아 교체한다(받기 실패 시 기존 커버는 그대로 남는다)
                    await comicinfo.download_cover_image(session, webtoon_dir, info, settings.request_timeout_seconds)
                job_status.log_line("metadata_sync", f"[{wt.title}] info.xml 갱신 / 커버 이미지 교체")
                fixed += 1
            except Exception as e:
                log.error("메타 동기화 중 예외 (titleId=%s) — 다음 웹툰으로 진행: %s", wt.title_id, e)
                job_status.log_line("metadata_sync", f"[{wt.title}] 처리 중 오류: {e}")
    return fixed


async def sync_kakao_metadata(client: kakao_page_download.KakaoPageClient, settings: Settings) -> int:
    """
    추적 중인 카카오페이지 웹툰(구독/구독해제/목록/제외됨 전부)의 작품 폴더에서 info.xml과 커버를 카카오페이지의 최신 정보로
    교체한다. 예전 카카오웹툰 시절에 받은 커버(다른 확장자 포함)와 info.xml도 이걸로 카카오페이지 것으로 바뀐다. 폴더가 없으면
    (받은 적 없는 작품) 건너뛰고 카카오에 묻지도 않는다. 작품 정보를 못 받으면 그 작품의 기존 파일은 그대로 두고, 표지만 못
    받으면 info.xml만 갱신하고 기존 커버를 유지한다. 한 작품에서 예외가 나도 나머지는 계속한다. info.xml을 갱신한 작품 수를 돌려준다.
    """
    root = Path(download_roots.kakao_root(settings))
    targets = [(row, root / remove_forbidden_str_kakao(row["title"])) for row in repository.get_kakao_webtoons_map().values()]
    targets = [(row, folder) for row, folder in targets if folder.is_dir()]
    if not targets:
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)

    async def _run_one(row: dict, folder: Path) -> bool:
        title, series_id = row["title"], row["title_id"]
        async with semaphore:
            try:
                series_item = await client.fetch_series_item(series_id)
                about = await client.fetch_about(series_id)
                if series_item is None:
                    job_status.log_line("metadata_sync", f"[카카오] {title} — 작품 정보 조회 실패로 건너뜀")
                    return False
                kakao_page_download.write_info_xml(series_item, series_id, folder, about)
                replaced = await asyncio.to_thread(kakao_cover.replace_cover, folder, series_id)
                job_status.log_line(
                    "metadata_sync", f"[카카오] {title} — info.xml 갱신 / " + ("커버 교체" if replaced else "커버는 받지 못해 기존 유지")
                )
                return True
            except Exception as e:
                log.error("카카오 메타 동기화 중 예외 (series_id=%s) — 다음 작품으로 진행: %s", series_id, e)
                job_status.log_line("metadata_sync", f"[카카오] {title} — 처리 중 오류: {e}")
                return False
            finally:
                await asyncio.sleep(settings.delay_seconds)

    results = await asyncio.gather(*(_run_one(row, folder) for row, folder in targets))
    return sum(1 for r in results if r)


async def refresh_inactive_metadata(session: aiohttp.ClientSession, settings: Settings) -> int:
    """
    구독중(active)이 아닌(구독해제/제외됨) 웹툰들의 정보(태그/장르/휴재/신작 등)를
    가볍게 갱신한다. 신작 스캔(scan_subscriptions_for_updates)은 구독중인 것만
    처리하기 때문에, 구독해제/제외됨은 사용자가 수동으로 "지금 전체 재동기화"를
    누르지 않는 한 계속 예전 정보(빈 태그, 신작 여부 등)로 남아있는 문제가 있었다 —
    이걸 신작 스캔이 돌 때마다 자동으로 같이 처리해서, 수동 재동기화가 항상 필요하지
    않도록 한다. 저자를 "등록된 작가"로 올리지는 않는다(구독한 게 아니므로).
    """
    targets = [wt for wt in repository.list_all() if wt.status != repository.STATUS_ACTIVE]
    if not targets:
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)

    async def _run_one(wt) -> bool:
        async with semaphore:
            success, _message = await enrich_one(session, wt.title_id, settings, register_authors_enabled=False)
            await asyncio.sleep(settings.delay_seconds)
            return success

    results = await asyncio.gather(*(_run_one(wt) for wt in targets))
    return sum(1 for r in results if r)


async def refresh_inactive_kakao_status(settings: Settings, *, all_batches: bool = False, progress: Callable[[str], None] | None = None) -> int:
    """구독 중이 아닌(구독해제/제외됨) 카카오 작품의 연재 상태(완결/휴재/서비스 종료)를 알아내 저장한다. 카카오 요일 목록은 연재 중인 작품만 담고
    완결 여부는 다운로드를 끝냈을 때만 저장돼서, 받지 않는 작품은 완결이나 서비스가 끝난 작품도 제외됨에 그대로 보였다.
    아직 안 알아본(또는 다시 볼 때가 된) 작품만 묻는다 — 기본(신작 스캔)은 한 번에 LOOKUP_BATCH개까지만 하고 다음 스캔에서 이어서 한다.
    all_batches=True("지금 전체 재동기화")면 LOOKUP_BATCH개씩 끝까지 이어서 하고, 묶음 사이에 BATCH_REST_SECONDS만큼 쉰다(카카오에 몰리지 않게).
    묶음마다 progress("N/전체")로 진행 상황을 알린다. 못 알아낸 작품도 결과가 저장돼(일시 오류는 하루 동안 다시 안 묻는다) 목록이 줄어들므로 끝나지 않는 일은 없다.
    완결(on_issue N)은 DB에 완결로 저장하고(다시 연재하면 해제), 작품 정보가 없으면(404/410) 서비스 종료로 기록한다. 일시 오류는 상태를 바꾸지 않는다.
    상태를 알아낸 작품 수를 돌려준다."""
    inactive_ids = [row["title_id"] for row in repository.get_kakao_webtoons_map().values() if row["status"] != repository.STATUS_ACTIVE]
    pending = kakao_status.pending_ids(inactive_ids)
    total = len(pending)
    if not pending:
        return 0
    resolved = done = 0
    listed_ids = {item["title_id"] for item in kakao_catalog.snapshot()[0] if item.get("weekdays")}  # 요일별 목록에 있는 작품은 요일을 이미 안다 — 개요를 묻지 않는다
    async with kakao_page_download.new_session() as kakao_session:
        client = kakao_page_download.client_or_anonymous(kakao_session, settings.request_timeout_seconds)
        while pending:
            batch = pending[: kakao_status.LOOKUP_BATCH]
            for series_id in batch:
                resolved += await refresh_one_kakao_status(client, series_id, listed_ids)
                await asyncio.sleep(kakao_status.LOOKUP_PAUSE_SECONDS)
            done += len(batch)
            if progress is not None:
                progress(f"카카오 구독해제/제외됨 상태 확인 {done}/{total}개 진행")
            if not all_batches:
                break
            pending = kakao_status.pending_ids(inactive_ids)  # 방금 확인한 작품은 빠진다
            if pending:
                await asyncio.sleep(kakao_status.BATCH_REST_SECONDS)
    return resolved


async def refresh_one_kakao_status(client, series_id: int, listed_ids: set[int]) -> int:
    """작품 하나의 상태(연재/휴재/완결/서비스 종료/판매 중지)를 확인해 저장한다. 알아냈으면 1, 못 알아냈으면 0(일시 오류 — 하루 뒤에 다시). 한 작품이 예외로 실패해도 밖으로 던지지 않는다.
    요일별 목록(listed_ids)에 없는 연재/휴재 작품은 작품 개요에서 연재 요일도 알아낸다(판매 중지/완결/종료는 요일이 의미 없어 묻지 않는다)."""
    try:
        http, series_item = await client.fetch_series_status(series_id)
    except Exception as e:
        log.warning("카카오 작품 상태 확인 실패 (series_id=%s): %s", series_id, e)
        http, series_item = None, None
    on_issue = (series_item or {}).get("on_issue")
    state = (series_item or {}).get("state")
    days: list[str] = []
    if on_issue in ("Y", "P") and not kakao_status.is_sale_stopped(state) and series_id not in listed_ids:
        try:
            days = kakao_status.parse_pub_period(((await client.fetch_overview(series_id)) or {}).get("pub_period"))
        except Exception as e:
            log.warning("카카오 작품 개요 확인 실패 (series_id=%s): %s", series_id, e)
    kakao_status.save(series_id, on_issue=on_issue, http=http, state=state, weekdays=days)
    if on_issue is not None:
        repository.set_kakao_finished(series_id, on_issue == "N")
    return 1 if on_issue is not None or http in kakao_status.ENDED_HTTP else 0


async def resync_registry(session: aiohttp.ClientSession, settings: Settings) -> int:
    """
    지금 추적 중인(구독중/구독해제) 모든 웹툰을 한 번에 훑어서 작가/태그 레지스트리를
    채운다. enrich_one은 구독하는 "그 순간"에만 동작하는데, 그 기능이 생기기 전에
    이미 구독해뒀던 웹툰들은 정기 스캔이 돌기 전까지 계속 비어있는 문제가 있어서
    — 설정 페이지에서 사용자가 바로 눌러서 즉시 채울 수 있게 하는 수동 트리거다.

    각 웹툰의 처리 결과(성공/실패 이유)를 job_status 로그에 남겨서, "재동기화는
    끝났다는데 왜 목록이 비었지?"를 실행 이력에서 바로 진단할 수 있게 한다.
    """
    targets = repository.list_all()  # 제외됨도 포함 — 구독 상태와 무관하게 저자/태그 정보는 채워야 함
    if not targets:
        job_status.log_line("registry", "재동기화 대상 웹툰이 없습니다")
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)

    async def _run_one(wt) -> bool:
        async with semaphore:
            # Metadata refresh never changes the user's author selections.
            success, message = await enrich_one(
                session, wt.title_id, settings, register_authors_enabled=False, preserve_author_selection=True
            )
            job_status.log_line("registry", f"[{wt.title}] {message}")
            await asyncio.sleep(settings.delay_seconds)
            return success

    results = await asyncio.gather(*(_run_one(wt) for wt in targets))
    return sum(1 for r in results if r)


async def resync_kakao_registry(client: kakao_page_download.KakaoPageClient, settings: Settings) -> int:
    """
    지금 추적 중인 모든 카카오페이지 웹툰(구독/구독해제/목록/제외됨 전부)의 작품 "정보"(글/그림/원작)를 받아서
    ① 아카이빙 파일명 템플릿 {author}용 글 작가를 저장하고 ② 관심 작가로 등록한다(네이버 재동기화와 같은 규칙):
    원작자가 있으면 원작자, 없으면 글 작가(그림 작가는 안 함). 새 후보는 꺼진 상태로 추가하며,
    기존 항목의 켜짐/꺼짐은 작품 상태나 자동 등록 설정과 무관하게 유지한다. 구독할 때 작품 정보를
    못 받았거나, 이 기능이 생기기 전에 구독해 둔 작품을 채우는 수동 트리거다.
    작품 하나가 실패해도(조회 실패/예외) 나머지는 계속하고, 결과를 작품마다 실행 이력에 남긴다. 정보 갱신에 성공한 작품 수를 돌려준다.
    """
    rows = list(repository.get_kakao_webtoons_map().values())
    if not rows:
        job_status.log_line("registry", "재동기화 대상 카카오 웹툰이 없습니다")
        return 0

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)

    async def _run_one(row: dict) -> bool:
        async with semaphore:
            title = row["title"]
            try:
                about = await client.fetch_about(row["title_id"])
            except Exception as e:
                log.warning("카카오 작품 정보(series_id=%s) 조회 예외: %s", row["title_id"], e)
                about = None
            await asyncio.sleep(settings.delay_seconds)
            if about is None:
                job_status.log_line("registry", f"[카카오] {title} — 작품 정보 조회 실패")
                return False
            writers, _, originals = kakao_authors.split_authors(about)
            repository.set_kakao_authors(row["title_id"], writers, originals)
            names = kakao_authors.authors_to_register(about)
            if not names:
                job_status.log_line("registry", f"[카카오] {title} — 작가 정보 없음")
                return False
            for name in names:
                repository.upsert_watched_author(name, name, enabled=False, platform="kakao", preserve_selection=True)
            label = "작가 정보 갱신(선택 유지)"
            job_status.log_line("registry", f"[카카오] {title} — {label}: {', '.join(names)}")
            return True

    results = await asyncio.gather(*(_run_one(row) for row in rows))
    return sum(1 for r in results if r)


async def _add_discovered_titles(
    session: aiohttp.ClientSession, settings: Settings, others: list[dict], enabled_author_ids: set[str]
) -> list[tuple[str, str]]:
    """Each watched author has its own baseline; current selection is checked on admission."""
    added = []
    hidden = await daily_plus.current_ids(settings.request_timeout_seconds, session)
    authors = {a.author_id: a.author_name for a in repository.list_watched_authors() if a.enabled}
    for source_id in sorted(enabled_author_ids & authors.keys()):
        matches = [o for o in others if o.get("titleId") is not None and _matches_enabled_writer(o, {source_id})]
        candidates = auto_subscribe.observe("naver", repository.SOURCE_ARTIST, source_id, authors[source_id],
            [{"title_id": str(o["titleId"]), "title": o.get("titleName", "")} for o in matches])
        pending = {r["title_id"] for r in candidates}
        for other in matches:
            tid = str(other["titleId"])
            if tid not in pending or tid in hidden or not auto_subscribe.is_enabled():
                continue
            try:
                if await _is_other_finished(session, other, tid, settings):
                    continue
                policy, scope = await _automatic_scope("naver", tid, settings)
                title = other.get("titleName", "")
                if not auto_subscribe.add_naver(tid, title, repository.SOURCE_ARTIST, source_ids=[source_id], scope=scope, policy=policy):
                    continue
                await enrich_one(session, tid, settings, register_authors_enabled=repository.is_author_auto_register_enabled())
                added.append((tid, title))
            except Exception as exc:
                log.warning("네이버 자동 구독 보류 (%s): %s", tid, exc)
    return added


async def exclude_auto_added_daily_plus(session: aiohttp.ClientSession, settings: Settings) -> list[tuple[str, str]]:
    """신작 스캔이 이미 자동으로 구독 추가해 둔 네이버 매일+ 작품을 "제외됨"으로 정리한다(직접 구독한 작품은 그대로). (title_id, 제목) 목록을 돌려준다.
    매일+ 목록을 모르면(빈 목록) 아무것도 하지 않는다."""
    hidden = await daily_plus.current_ids(settings.request_timeout_seconds, session)
    excluded = repository.exclude_auto_added_titles(hidden)
    for title_id, title in excluded:
        log.info("자동 추가된 매일+ 작품을 제외됨으로 정리: %s (%s)", title, title_id)
    return excluded


async def scan_subscriptions_for_updates(session: aiohttp.ClientSession, settings: Settings) -> None:
    """구독 중인(status=active) 모든 웹툰의 완결/휴재/장르 갱신."""
    active_webtoons = repository.list_by_status(repository.STATUS_ACTIVE)
    if not active_webtoons:
        return

    semaphore = asyncio.Semaphore(settings.artist_scan_concurrency)
    tasks = [
        _fetch_info_and_others(session, semaphore, wt.title_id, settings) for wt in active_webtoons
    ]
    fetch_results = await asyncio.gather(*tasks)

    for title_id, info, others in fetch_results:
        if info is None:
            continue

        if info.is_finished:
            repository.mark_finished(title_id)

        repository.update_is_adult(title_id, info.is_adult)
        repository.update_thumbnail_url(title_id, info.thumbnail_url)
        repository.update_genres_and_tags(title_id, info.genres_ko, info.tags)
        repository.update_is_paused(title_id, info.is_paused)
        repository.update_is_new(title_id, info.is_new)

        if info.writer_id_name_pairs:
            ids, names = zip(*info.writer_id_name_pairs)
            repository.update_writer_ids_and_names(title_id, list(ids), list(names))

        _store_origin_authors(title_id, info)

        # 새 후보의 켜짐은 설정을 따르고, 기존 직접 선택은 upsert가 유지한다.
        for author_id, author_name in authors_to_register(info):
            repository.upsert_watched_author(author_id, author_name, enabled=repository.is_author_auto_register_enabled())



async def discover_titles_for_unlinked_watched_authors(session: aiohttp.ClientSession, settings: Settings) -> list[str]:
    """등록된 작가 신작 자동추가는 원래 "구독 중인 작품을 스캔하면서 그 작가의 다른
    작품을 같이 조회"하는 방식이라, 그 작가의 작품을 하나도 구독하고 있지 않으면
    (오랫동안 활동이 없던 작가를 등록해둔 경우 등) 전혀 트리거되지 않는 문제가 있었다.

    이 함수는 등록된 작가 전부를 대상으로 이름으로 다시 검색해서, 정확히 같은
    author_id를 가진 작품을 하나(anchor) 찾은 뒤, 거기서부터 "다른 작품" 목록을
    가져와 동일한 조건으로 자동 구독한다 — 구독 여부와 무관하게 항상 동작한다.
    새로 추가된 작품의 "- 제목 (`id`)" 로그 줄 리스트를 반환한다."""
    authors = [a for a in repository.list_watched_authors() if a.enabled]
    if not authors:
        return []

    new_entry_lines: list[str] = []

    for author in authors:
        try:
            search_results = await naver_api.search_webtoons(session, author.author_name, settings.request_timeout_seconds)
        except Exception as e:
            log.error("작가 재검색 중 예외 (author_id=%s): %s", author.author_id, e)
            continue

        anchor_title_id = None
        for item in search_results:
            if any(aid == author.author_id for aid, _name in item.author_ids_names):
                anchor_title_id = item.title_id
                break
        if anchor_title_id is None:
            continue  # 이 작가로 검색해도 작품을 못 찾음 — 이름이 바뀌었거나 활동이 뜸한 경우

        try:
            others = await naver_api.fetch_other_titles_by_artist(session, anchor_title_id, settings.request_timeout_seconds)
        except Exception as e:
            log.error("작가의 다른 작품 조회 중 예외 (author_id=%s): %s", author.author_id, e)
            job_status.log_line("discovery", f"[{author.author_name}] 작품 조회 실패 — 기준선 유지: {e}")
            continue

        others += [{"titleId": item.title_id, "titleName": item.title_name,
                    "author": {"writers": [{"id": aid} for aid, _ in item.author_ids_names]}}
                   for item in search_results if any(aid == author.author_id for aid, _ in item.author_ids_names)]
        added = await _add_discovered_titles(session, settings, others, {author.author_id})
        new_entry_lines.extend(f"- {name} (`{tid}`)" for tid, name in added)
        await asyncio.sleep(settings.delay_seconds)

    if new_entry_lines:
        message = "🆕 **작가 신작 자동 추가 (미구독 작가)**\n" + "\n".join(new_entry_lines)
        await send_webhook_notification(session, message)

    return new_entry_lines


async def scan_curation_tags(session: aiohttp.ClientSession, settings: Settings) -> None:
    """Discover tag candidates independently of automatic subscription consent."""
    new_entry_lines = []
    tag_ids = repository.get_enabled_tag_ids()
    hidden = await daily_plus.current_ids(settings.request_timeout_seconds, session) if tag_ids else set()
    for tag_id in tag_ids:
        try:
            tag_name = await naver_api.fetch_curation_title_name(session, int(tag_id), settings.request_timeout_seconds)
            repository.upsert_watched_tag(tag_id, tag_name, enabled=False)
            items = await naver_api.fetch_curation_titles(session, int(tag_id), settings.request_timeout_seconds, settings.delay_seconds)
            candidates = auto_subscribe.observe("naver", repository.SOURCE_TAG, tag_id, tag_name,
                [{"title_id": str(o["titleId"]), "title": o.get("titleName", "")} for o in items if o.get("titleId") is not None])
            pending = {r["title_id"] for r in candidates}
            for item in items:
                tid = str(item.get("titleId", ""))
                if tid not in pending or tid in hidden or item.get("finished") or not auto_subscribe.is_enabled():
                    continue
                try:
                    policy, scope = await _automatic_scope("naver", tid, settings)
                    title = item.get("titleName", "")
                    if not auto_subscribe.add_naver(tid, title, repository.SOURCE_TAG, source_ids=[tag_id], scope=scope, policy=policy):
                        continue
                    await enrich_one(session, tid, settings, register_authors_enabled=repository.is_author_auto_register_enabled())
                    new_entry_lines.append(f"- {title} (`{tid}`) [태그: {tag_name}]")
                except Exception as exc:
                    log.warning("태그 자동 구독 보류 (%s): %s", tid, exc)
        except Exception as exc:
            log.error("태그 스캔 오류 (%s): %s", tag_id, exc)
            job_status.log_line("discovery", f"[태그 {tag_id}] 작품 조회 실패 — 기준선 유지: {exc}")
    if new_entry_lines:
        await send_webhook_notification(session, "🆕 **태그 신작 자동 추가**\n" + "\n".join(new_entry_lines))
