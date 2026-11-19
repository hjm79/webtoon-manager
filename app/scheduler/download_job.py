"""다운로드 작업 — 네이버와 카카오페이지를 한 번에 돌리고 이력 정리와 실패 알림을 한다.
"""

import asyncio
import logging
import time

import aiohttp

from app import cookie_health, daily_plus, job_status, kakao_page_auth, repository, retention, waitfree_sync, workflow_progress
from app.config import get_settings

from app.scheduler.kakao_download import (
    _download_kakao_subscriptions,
)
from app.scheduler.naver_download import (
    _download_new_episodes_for_one,
)

log = logging.getLogger(__name__)

# 정기 스케줄 실행과 "수동 실행" 버튼이 겹치는 걸 막는 잡별 락 (동시성 문제 방지).
_download_job_lock = asyncio.Lock()


_DOWNLOAD_TARGET_PLATFORMS = {"naver": {"naver"}, "kakao": {"kakao"}, "both": {"naver", "kakao"}}


_download_claimed_platforms: set[str] = set()  # 지금 실행 중이거나 순서를 기다리는 실행이 맡은 플랫폼
_single_title_tasks: set[asyncio.Task] = set()
_queue_worker_task: asyncio.Task | None = None
_active_batch_titles: set[tuple[str, str]] = set()


def title_running(platform: str, title_id: str) -> bool:
    if (platform, str(title_id)) in _active_batch_titles:
        return True
    progress = workflow_progress.snapshot()
    return progress.get("platform") == platform and str(progress.get("title_id")) == str(title_id) \
        and progress.get("state") in ("running", "resting")


def title_queued(platform: str, title_id: str) -> bool:
    return any(row["platform"] == platform and row["title_id"] == str(title_id)
               for row in repository.list_download_queue())


def start_queued_downloads(*, recover: bool = False) -> None:
    global _queue_worker_task
    if recover:
        repository.update_download_queue(recover=True)
        kakao_page_auth.resume_verified_queue()
    if _queue_worker_task is not None and not _queue_worker_task.done():
        return
    if not repository.list_download_queue():
        return
    _queue_worker_task = asyncio.create_task(_run_queued_downloads())


async def stop_queued_downloads() -> None:
    if _queue_worker_task is not None and not _queue_worker_task.done():
        _queue_worker_task.cancel()
        try:
            await _queue_worker_task
        except asyncio.CancelledError:
            pass


async def _run_queued_downloads() -> None:
    from app import kakao_page_download

    while items := repository.list_download_queue():
        if download_busy() or kakao_page_download.download_lock.locked():
            await asyncio.sleep(1)
            continue
        eligible = [row for row in items if row['state'] == 'queued' or
                    (row['state'] == 'retry_wait' and (row.get('retry_at') or 0) <= time.time())]
        if not eligible:
            if any(row['state'] == 'retry_wait' for row in items):
                await asyncio.sleep(1)
                continue
            return
        item = eligible[0]
        platform, title_id = item["platform"], item["title_id"]
        reserve_title_download(platform)
        try:
            repository.update_download_queue(item["id"], state="running")
            async with _download_job_lock:
                await _run_download_job_impl({platform}, only_title=title_id)
        except kakao_page_auth.KakaoAuthPending as exc:
            state, message = kakao_page_auth.defer_download(item['id'], exc)
            workflow_progress.update(state=state, error=message, rest_until=None)
            continue
        except asyncio.CancelledError:
            workflow_progress.fail("다운로드가 중단되었습니다. 다음 실행에서 이어받습니다.")
            job_status.finish("download", success=False)
            raise
        except Exception:
            log.exception("대기열 작품 다운로드 처리 중 오류")
            workflow_progress.fail("다운로드 처리 중 오류가 발생했습니다. 다음 대기 작품으로 진행합니다.")
            job_status.finish("download", success=False)
        finally:
            _active_batch_titles.clear()
            release_title_download(platform)
        repository.update_download_queue(item["id"])


def download_busy() -> bool:
    return bool(_download_claimed_platforms) or _download_job_lock.locked() or any(
        job_status.snapshot()[name]["status"] == "running" for name in ("manual",)
    )


def reserve_title_download(platform: str) -> None:
    from app import kakao_page_download

    if download_busy() or kakao_page_download.download_lock.locked():
        raise ValueError("다른 다운로드가 실행 중입니다. 완료 후 다시 시도해주세요.")
    _download_claimed_platforms.add(platform)


def release_title_download(platform: str) -> None:
    _download_claimed_platforms.discard(platform)


def enqueue_reserved_title(platform: str, title_id: str, title: str) -> None:
    workflow_progress.begin(platform, title_id, title, 0)
    workflow_progress.update(state="queued")

    async def run_one():
        try:
            async with _download_job_lock:
                await _run_download_job_impl({platform}, only_title=title_id)
        except asyncio.CancelledError:
            workflow_progress.fail("다운로드가 중단되었습니다. 다음 실행에서 이어받습니다.")
            job_status.finish("download", success=False)
            raise
        except Exception:
            log.exception("한 작품 다운로드 처리 중 오류")
            workflow_progress.fail("다운로드 처리 중 오류가 발생했습니다. 실행 이력을 확인해주세요.")
            job_status.finish("download", success=False)
        finally:
            _active_batch_titles.clear()
            release_title_download(platform)

    task = asyncio.create_task(run_one())
    _single_title_tasks.add(task)
    task.add_done_callback(_single_title_tasks.discard)


async def run_download_job(target: str = "naver", scheduled: bool = False) -> None:
    """target: naver | kakao | both(스케줄에서 고른 대상). 정기 스케줄과 '수동 실행' 버튼이 동시에 같은 플랫폼을
    받으려 하면 같은 웹툰을 두 실행이 동시에 다운로드할 수 있다(회차 저장/압축이 원자적이지 않음) — 이미 실행 중이거나
    기다리는 실행이 같은 플랫폼을 맡고 있으면 조용히 건너뛴다(에러 아님, "이미 실행 중"으로 로그만 남김). 서로 다른
    플랫폼(예: 네이버 스케줄과 카카오 스케줄이 같은 시각)은 건너뛰지 않고 순서를 기다렸다가 실행한다.
    scheduled: 정기 스케줄이 부른 것(True)이면 "기다무 충전 시각에 맞춰 받기"가 켜져 있을 때 충전 시각 전인 카카오 기다무 작품을 건너뛴다. 수동 실행은 항상 전부 확인한다."""
    wanted = set(_DOWNLOAD_TARGET_PLATFORMS.get(target, {"naver"}))
    platforms = wanted - _download_claimed_platforms
    if job_status.snapshot()["manual"]["status"] == "running":
        platforms.discard("naver")
    if not platforms:
        job_status.log_line("download", "이미 실행 중이거나 대기 중이라 건너뜁니다 (중복 실행 방지)")
        return
    _download_claimed_platforms.update(platforms)
    try:
        async with _download_job_lock:
            await _run_download_job_impl(platforms, scheduled, partial=platforms != wanted)
    finally:
        _active_batch_titles.clear()
        _download_claimed_platforms.difference_update(platforms)


async def _run_download_job_impl(platforms: set[str], scheduled: bool = False, partial: bool = False,
                                 only_title: str | None = None) -> None:
    """partial: 일부 플랫폼은 이미 다른 실행이 맡고 있어서 나머지만 실행하는 경우 — 안내 줄은 job_status.start가 로그를 비운 뒤에 남겨야 사라지지 않는다."""
    settings = get_settings()
    # 신작 스캔이 자동으로 구독 추가해 둔 매일+ 작품은 받지 않는다 — 저장해 둔 매일+ 목록으로(네트워크 없이) 먼저 제외됨으로 정리한다
    auto_excluded = repository.exclude_auto_added_titles(daily_plus.cached_ids()) if "naver" in platforms else []
    active_webtoons, active_kakao_webtoons = repository.download_subscription_snapshot(platforms, include_queued=only_title is not None)
    if only_title is not None:
        active_webtoons = [w for w in active_webtoons if w.title_id == only_title]
        active_kakao_webtoons = [w for w in active_kakao_webtoons if str(w["title_id"]) == only_title]
    _active_batch_titles.update(("naver", w.title_id) for w in active_webtoons)
    _active_batch_titles.update(("kakao", str(w["title_id"])) for w in active_kakao_webtoons)

    job_status.start("download")
    if partial:
        job_status.log_line("download", "이미 실행 중이거나 대기 중인 대상은 건너뛰고 나머지만 실행합니다 (중복 실행 방지)")
    if auto_excluded:
        job_status.log_line("download", f"자동 추가된 매일+ 작품 {len(auto_excluded)}개를 제외됨으로 정리했습니다: " + ", ".join(title for _, title in auto_excluded))
    if "naver" in platforms:
        job_status.log_line("download", f"다운로드 스캔 시작 — 구독 중인 네이버 웹툰 {len(active_webtoons)}개")
    else:
        job_status.log_line("download", "다운로드 스캔 시작 — 카카오페이지만")
    had_error = False

    adult_tracker = cookie_health.AdultFetchTracker()
    failures: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for webtoon in active_webtoons:
            try:
                await _download_new_episodes_for_one(session, settings, webtoon.title_id, adult_tracker, failures)
            except Exception as e:
                had_error = True
                log.error("웹툰(titleId=%s) 다운로드 처리 중 예외 — 다음 웹툰으로 진행: %s", webtoon.title_id, e)
                job_status.log_line("download", f"[{webtoon.title}] 처리 중 오류: {e}")
                failures.append({"title_name": webtoon.title, "episode_no": None, "subtitle": str(e)})
                workflow_progress.fail("작품 다운로드 처리 중 오류가 발생했습니다.")
            # 새 회차가 없어서 그냥 넘어가는 웹툰들 사이에도 딜레이를 둔다 — 예전엔 이
            # 경우에만 딜레이가 전혀 없어서, 구독 웹툰이 많으면 순식간에 연속 요청이
            # 나가는 문제가 있었다(실제로 코드 확인 후 발견됨).
            await asyncio.sleep(settings.delay_seconds)

        try:
            await cookie_health.finalize_and_notify(session, adult_tracker)
        except Exception as e:
            log.error("쿠키 상태 판단/알림 중 예외: %s", e)

        try:
            if "kakao" in platforms:
                await _download_kakao_subscriptions(settings, failures, only=active_kakao_webtoons,
                    skip_waiting=scheduled and waitfree_sync.is_enabled())
        except kakao_page_auth.KakaoAuthPending as exc:
            workflow_progress.update(state='retry_wait' if exc.state in ('unknown', 'credentials_changed') else 'blocked_auth', error=str(exc))
            if only_title is not None:
                job_status.finish('download', success=False)
                raise
            had_error = True
        except Exception as e:
            had_error = True
            log.error("카카오페이지 자동 다운로드 중 예외: %s", e)
            job_status.log_line("download", f"카카오페이지 자동 다운로드 중 오류: {e}")

    if failures:
        # 실패한 작품/회차는 리포트(다운로드가 끝나면 모아서 발송)에 담기지만, 이번 실행 로그에도 한 줄로 요약해 둔다
        job_status.log_line("download", f"실패 {len(failures)}건: " + ", ".join(_failure_label(f) for f in failures))

    try:
        _cleanup_old_episode_history()
    except Exception as e:
        log.error("다운로드 이력 보관기간 정리 중 예외: %s", e)

    try:
        _cleanup_old_job_history()
    except Exception as e:
        log.error("실행 이력 보관기간 정리 중 예외: %s", e)

    job_status.log_line("download", "다운로드 스캔 종료")
    job_status.finish("download", success=not had_error and not failures)


def _cleanup_old_job_history() -> None:
    """설정된 보관 기간(일)보다 오래된 실행 이력(수동실행/신작스캔/다운로드 등 잡 실행 기록)을 정리한다. 설정 안 했으면 아무것도 안 함."""
    retention.cleanup_history("download", "job_history_retention_days", repository.delete_job_history_older_than, "실행 이력")


def _cleanup_old_episode_history() -> None:
    """설정된 보관 기간(일)보다 오래된 다운로드 이력을 정리한다(파일은 그대로 유지됨). 설정 안 했으면(0 또는 미설정) 아무것도 안 함."""
    retention.cleanup_history("download", "episode_history_retention_days", repository.delete_episode_history_older_than, "다운로드 이력")


def _failure_label(failure: dict) -> str:
    """실패 한 건을 로그 한 줄에 들어갈 짧은 글로 — 회차가 있으면 "작품 12화", 없으면(작품 단위 오류) "작품(이유)"."""
    if failure["episode_no"] is not None:
        return f"{failure['title_name']} {failure['episode_no']}화"
    return f"{failure['title_name']}({failure['subtitle']})"

