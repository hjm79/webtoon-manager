"""Completed ZIP batches request scanning before a long job ends, without report events."""
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import db, job_status, kakao_page_download as kp, report_dispatch, repository, viewer_scan, workflow_progress
db.get_connection()
from app.scheduler import kakao_download as kd, naver_download as nd

ROOT = Path(tempfile.mkdtemp())
SETTINGS = NS(delay_seconds=0, request_timeout_seconds=5, max_new_episodes_per_title=1,
              batch_rest_minutes=7, download_root=str(ROOT), folder_zero_fill=4,
              image_zero_fill=3, max_concurrent_downloads=2)


async def nothing(*args, **kwargs): pass


async def naver_boundaries():
    repository.upsert_new(title_id="601", title="배치 경계")
    folder = ROOT / "배치 경계"
    folder.mkdir()
    ep = lambda number: NS(episode_no=number, subtitle=f"{number}화")
    plan = nd._NaverDownloadPlan(NS(title_name="배치 경계", webtoon_type="WEBTOON"), {}, folder, [ep(1), ep(2), ep(3)])
    requests, attempts, rests = [], [], []
    async def download(**kwargs):
        attempts.append(kwargs["episode"].episode_no)
        return True, None
    def archive(*args, **kwargs):
        path = folder / f"{attempts[-1]}.zip"
        path.write_bytes(b"complete")
        return [path]
    async def rest(platform, title_id, seconds):
        assert len(requests) == 1, "The first batch must be queued for scan before its long rest"
        assert attempts == [1], "The whole title must still be unfinished"
        rests.append(seconds)
        repository.set_status(title_id, repository.STATUS_UNSUBSCRIBED)
    with patch.object(nd, "download_single_episode", download), patch.object(nd, "zip_episode_folders", archive), \
         patch.object(nd.download_cooldown, "rest", rest), patch.object(nd.asyncio, "sleep", nothing), \
         patch.object(viewer_scan, "request", requests.append), patch.object(report_dispatch, "note_download_finished") as report:
        await nd._download_pending_batches(None, SETTINGS, "601", plan, [])
        assert len(requests) == 1 and isinstance(requests[0], datetime)
        assert rests == [420] and workflow_progress.title_snapshot("naver", "601")["state"] == "deferred"
        report.assert_not_called()

    # Mid-batch unsubscribe flushes the completed first ZIP once in the title-exit finally block.
    repository.set_status("601", repository.STATUS_ACTIVE)
    attempts.clear(); requests.clear()
    SETTINGS.max_new_episodes_per_title = 3
    async def unsubscribe_after_file(**kwargs):
        attempts.append(kwargs["episode"].episode_no)
        repository.set_status("601", repository.STATUS_UNSUBSCRIBED)
        return True, None
    with patch.object(nd, "download_single_episode", unsubscribe_after_file), patch.object(nd, "zip_episode_folders", archive), \
         patch.object(nd.asyncio, "sleep", nothing), patch.object(viewer_scan, "request", requests.append):
        await nd._download_pending_batches(None, SETTINGS, "601", plan, [])
    assert attempts == [1] and len(requests) == 1

    repository.set_status("601", repository.STATUS_ACTIVE)
    requests.clear()
    empty = nd._NaverDownloadPlan(plan.info, {}, folder, [])
    with patch.object(viewer_scan, "request", requests.append):
        await nd._download_pending_batches(None, SETTINGS, "601", empty, [])
    assert requests == [], "No completed ZIP means no scan request"

    # Scan bookkeeping failure cannot turn a safely finished ZIP into a failed download.
    attempts.clear()
    with patch.object(nd, "download_single_episode", download), patch.object(nd, "zip_episode_folders", archive), \
         patch.object(nd.asyncio, "sleep", nothing), patch.object(viewer_scan, "request", side_effect=OSError("db unavailable")):
        await nd._download_pending_batches(None, SETTINGS, "601", nd._NaverDownloadPlan(plan.info, {}, folder, [ep(4)]), [])
    assert workflow_progress.title_snapshot("naver", "601")["state"] == "success"


def kakao_batch():
    repository.upsert_new_kakao_webtoon(602, "카카오 배치", status=repository.STATUS_ACTIVE)
    webtoon = repository.get_kakao_webtoon(602)
    result = kp.RunResult([1], [(1, "1화")], None, kp.DownloadPlan(mode="new_folder"), title=webtoon["title"],
                          stopped_reason="구독 해제")
    requests = []
    with patch.object(viewer_scan, "request", requests.append), patch.object(report_dispatch, "note_download_finished") as report:
        kd._record_kakao_batch(webtoon, result, [])
        assert len(requests) == 1 and isinstance(requests[0], datetime)
        kd._record_kakao_batch(webtoon, kp.RunResult([], [], None, kp.DownloadPlan(mode="new_folder")), [])
        assert len(requests) == 1
        report.assert_not_called()
    with patch.object(viewer_scan, "request", side_effect=OSError("db unavailable")):
        kd._record_kakao_batch(webtoon, result, [])


async def main():
    job_status.start("download")
    await naver_boundaries()
    kakao_batch()


asyncio.run(main())
print("Fresh completed ZIPs queue scans at safe batch/exit boundaries; no changes and report timing remain untouched")
