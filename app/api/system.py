"""백업/복원, 도움말."""


import asyncio
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import backup_restore, help_page, job_status, kakao_catalog, kakao_page_download, kakao_records, repository
from app import scheduler as scheduler_mod
from app.config import get_settings
from app.scheduler import archive_job, download_job, report_job, report_tick


log = logging.getLogger(__name__)

router = APIRouter()
_restore_lock = asyncio.Lock()


def restore_in_progress() -> bool:
    return _restore_lock.locked()



@router.get("/help", response_class=HTMLResponse)
async def get_help_page():
    """"도움말" 탭에 보여줄 README(일반 사용자 설명서)를 목차가 붙은 HTML로 바꿔서 준다. 변환 규칙은 app/help_page.py."""
    readme_path = Path(__file__).resolve().parent.parent.parent / "README.md"
    try:
        text = await asyncio.to_thread(readme_path.read_text, encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse("<p>README.md를 찾을 수 없습니다.</p>", status_code=404)
    return HTMLResponse(help_page.render_help(text))


@router.get("/backup")
async def download_backup():
    return await asyncio.to_thread(repository.export_all)


@router.post("/restore")
async def restore_backup(data: dict, request: Request):
    """Restore without allowing a download or archive move to overlap the database swap."""
    from app.api.workflow import _subscription_lock

    jobs = job_status.snapshot()
    if (restore_in_progress() or _subscription_lock.locked() or download_job.download_busy()
            or kakao_page_download.download_lock.locked() or archive_job._archive_job_lock.locked()
            or report_tick._report_tick_lock.locked() or report_job._report_job_lock.locked()
            or any(jobs[name]["status"] == "running" for name in ("archive", "bulk_move"))):
        raise HTTPException(status_code=409, detail="작업이 진행 중입니다. 완료 후 백업을 복원해주세요.")

    report = None
    try:
        async with (
            _restore_lock, _subscription_lock, download_job._download_job_lock, archive_job._archive_job_lock,
            report_tick._report_tick_lock, report_job._report_job_lock,
        ):
            task = asyncio.create_task(_restore_and_reconfigure(data, request))
            try:
                report = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the locks until the SQLite worker and post-restore steps finish.
                try:
                    report = await task
                except Exception:
                    log.exception("요청 중단 뒤 백업 복원 실패")
                raise
    finally:
        if report is not None:
            try:
                # The worker parks jobs on unsafe mounts and rechecks them before accessing the provider.
                download_job.start_queued_downloads(recover=True)
            except Exception:
                backup_restore.record_post_restore_failure(report, "다운로드 대기열 재개")
    return report


async def _restore_and_reconfigure(data: dict, request: Request) -> dict:
    settings = get_settings()
    try:
        report = await asyncio.to_thread(backup_restore.restore_backup, data, settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail="백업 데이터가 데이터베이스 제약 조건을 충족하지 않습니다.") from e
    except Exception as e:
        log.exception("백업 데이터 저장 실패")
        raise HTTPException(status_code=500, detail="백업을 데이터베이스에 저장하지 못했습니다.") from e

    # 예전 백업에는 받은 회차 기록이 없다 — 복원한 폴더가 있으면 그 폴더와 아카이빙 이력으로 만든다(없으면 첫 자동 다운로드 때)
    if "kakao" not in report["download_mount_problems"]:
        try:
            await asyncio.to_thread(kakao_records.backfill_and_log, settings)
        except Exception:
            backup_restore.record_post_restore_failure(report, "카카오페이지 회차 기록 보완")
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        try:
            await asyncio.to_thread(scheduler_mod.reschedule_all, scheduler)
        except Exception:
            backup_restore.record_post_restore_failure(report, "예약 작업 다시 적용")
    try:
        if await asyncio.to_thread(repository.get_setting, "kakao_webtoons_enabled") == "1":
            kakao_catalog.start_refresh()
    except Exception:
        backup_restore.record_post_restore_failure(report, "카카오페이지 작품 목록 갱신 시작")
    return report
