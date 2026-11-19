"""백업/복원, 도움말."""


import asyncio
import logging
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

    async with (
        _restore_lock, _subscription_lock, download_job._download_job_lock, archive_job._archive_job_lock,
        report_tick._report_tick_lock, report_job._report_job_lock,
    ):
        task = asyncio.create_task(_restore_and_reconfigure(data, request))
        try:
            report = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling the request cannot interrupt the SQLite worker thread.
            # Keep the job locks until recovery and scheduler reconfiguration finish.
            try:
                await task
            except Exception:
                log.exception("요청 중단 뒤 백업 복원 실패")
            else:
                download_job.start_queued_downloads(recover=True)
            raise
    download_job.start_queued_downloads(recover=True)
    return report


async def _restore_and_reconfigure(data: dict, request: Request) -> dict:
    settings = get_settings()
    try:
        report = await asyncio.to_thread(backup_restore.restore_backup, data, settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        # 스키마가 안 맞는 백업(필수 컬럼 없음 등)은 DB 예외가 그대로 올라올 수 있다 —
        # 원문 그대로 500으로 흘리는 대신 "복원 실패"로 명확히 감싼다. 트랜잭션은
        # write_transaction이 이미 롤백했으므로 DB는 이전 상태 그대로 안전하다.
        raise HTTPException(status_code=400, detail=f"백업 파일 형식이 올바르지 않아 복원하지 못했습니다: {e}") from e

    # 예전 백업에는 받은 회차 기록이 없다 — 복원한 폴더가 있으면 그 폴더와 아카이빙 이력으로 만든다(없으면 첫 자동 다운로드 때)
    await asyncio.to_thread(kakao_records.backfill_and_log, settings)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await asyncio.to_thread(scheduler_mod.reschedule_all, scheduler)
    if await asyncio.to_thread(repository.get_setting, "kakao_webtoons_enabled") == "1":
        kakao_catalog.start_refresh()  # 복원한 환경은 저장돼 있던 목록 캐시가 비어 있거나 예전 것이라 새로 채운다(화면은 기다리지 않는다)
    return report
