"""Fresh subscription checks at title, cooldown and episode boundaries preserve finished files."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import cookie_health, db, job_status, kakao_page_download as kp, repository, workflow_progress
from app.kakao_page_download import runner
from app.scheduler import kakao_download as kd, naver_download as nd

db.get_connection()
ROOT = Path(tempfile.mkdtemp())
SETTINGS = NS(delay_seconds=0, request_timeout_seconds=5, max_new_episodes_per_title=1,
              batch_rest_minutes=7, download_root=str(ROOT), folder_zero_fill=4,
              image_zero_fill=3, max_concurrent_downloads=2)


async def nothing(*args, **kwargs):
    return None


def episode(number):
    return kp.Episode(product_id=number, title=f"{number}화", number=number,
                      subtitle=f"{number}화", is_free=True, accessible=True, page_count=1, hidden=False)


async def naver_stops_after_resume():
    repository.upsert_new(title_id="301", title="네이버 휴식")
    async def resume(*args):
        repository.set_status("301", repository.STATUS_UNSUBSCRIBED)
    with patch.object(nd.download_cooldown, "resume", resume), patch.object(nd, "_plan_naver_download") as plan:
        await nd._download_new_episodes_for_one(None, SETTINGS, "301", cookie_health.AdultFetchTracker(), [])
    plan.assert_not_called()
    assert workflow_progress.title_snapshot("naver", "301")["state"] == "deferred"


async def naver_stops_after_current_file_and_rest():
    repository.upsert_new(title_id="302", title="네이버 회차")
    folder = ROOT / "naver"
    folder.mkdir()
    original = folder / "existing.zip"
    partial = folder / "untouched.part"
    original.write_bytes(b"existing")
    partial.write_bytes(b"partial")
    attempts = []
    async def single(**kwargs):
        attempts.append(kwargs["episode"].episode_no)
        repository.set_status("302", repository.STATUS_UNSUBSCRIBED)
        return True, folder / f"[{attempts[-1]:04d}]"
    def archive(_folder, **kwargs):
        path = folder / f"{attempts[-1]}.zip"
        path.write_bytes(b"finished")
        return [path]
    plan = nd._NaverDownloadPlan(NS(title_name="네이버 회차", webtoon_type="WEBTOON"), {}, folder,
                                 [NS(episode_no=n, subtitle=f"{n}화") for n in (2, 4)])
    with patch.object(nd, "download_single_episode", single), patch.object(nd, "zip_episode_folders", archive), \
         patch.object(nd.asyncio, "sleep", nothing):
        await nd._download_pending_batches(None, SETTINGS, "302", plan, [])
    assert attempts == [2]
    assert (folder / "2.zip").read_bytes() == b"finished"
    assert original.read_bytes() == b"existing" and partial.read_bytes() == b"partial"
    assert repository.get("302").last_downloaded_no == 2
    assert workflow_progress.title_snapshot("naver", "302")["state"] == "deferred"

    repository.set_status("302", repository.STATUS_ACTIVE)
    attempts.clear()
    async def single_active(**kwargs):
        attempts.append(kwargs["episode"].episode_no)
        return True, folder / f"[{attempts[-1]:04d}]"
    rests = []
    async def rest(platform, title_id, seconds):
        rests.append(seconds)
        repository.set_status(title_id, repository.STATUS_UNSUBSCRIBED)
    with patch.object(nd, "download_single_episode", single_active), patch.object(nd, "zip_episode_folders", archive), \
         patch.object(nd.download_cooldown, "rest", rest), patch.object(nd.asyncio, "sleep", nothing):
        await nd._download_pending_batches(None, SETTINGS, "302", plan, [])
    assert attempts == [2] and rests == [7 * 60]
    assert workflow_progress.title_snapshot("naver", "302")["state"] == "deferred"

    # A title present in an earlier scheduled snapshot is checked again when reached.
    repository.upsert_new(title_id="303", title="나중 작품")
    repository.set_status("303", repository.STATUS_UNSUBSCRIBED)
    with patch.object(nd, "_plan_naver_download") as pending_plan:
        await nd._download_new_episodes_for_one(None, SETTINGS, "303", cookie_health.AdultFetchTracker(), [])
    pending_plan.assert_not_called()


async def kakao_stops_at_title_and_cooldown_boundaries():
    repository.upsert_new_kakao_webtoon(401, "카카오 휴식", status=repository.STATUS_ACTIVE)
    snapshot = repository.get_kakao_webtoon(401)
    async def resume(*args):
        repository.set_kakao_webtoon_status(401, repository.STATUS_UNSUBSCRIBED)
    with patch.object(kd.download_cooldown, "resume", resume), patch.object(kd.kakao_page_download, "run_download") as run:
        result = await kd._download_kakao_title(None, SETTINGS, snapshot, str(ROOT), [])
    run.assert_not_called()
    assert result.stopped_reason and workflow_progress.title_snapshot("kakao", 401)["state"] == "deferred"

    repository.set_kakao_webtoon_status(401, repository.STATUS_ACTIVE)
    calls, rests = [], []
    async def one_batch(*args, **kwargs):
        assert kwargs["should_continue"]()
        calls.append(kwargs["max_episodes"])
        return kp.RunResult([2], [(2, "2화")], None,
                            kp.DownloadPlan(mode="compare", to_download=[episode(2), episode(4)]), title="카카오 휴식")
    async def rest(platform, title_id, seconds):
        rests.append(seconds)
        repository.set_kakao_webtoon_status(title_id, repository.STATUS_UNSUBSCRIBED)
    with patch.object(kd.download_cooldown, "resume", nothing), patch.object(kd.download_cooldown, "rest", rest), \
         patch.object(kd.kakao_page_download, "run_download", one_batch):
        result = await kd._download_kakao_title(None, SETTINGS, snapshot, str(ROOT), [])
    assert calls == [1] and rests == [7 * 60]
    assert result.stopped_reason and result.nothing_left is False
    assert "구독 해제로 중단 1개" in kd._kakao_download_summary(1, [result], False)

    # Reusing the earlier batch snapshot cannot reactivate this unsubscribed title.
    with patch.object(kd.kakao_page_download, "run_download") as pending_run:
        skipped = await kd._download_kakao_title(None, SETTINGS, snapshot, str(ROOT), [])
    pending_run.assert_not_called()
    assert skipped.stopped_reason


async def kakao_finishes_current_file_and_manual_stays_available():
    repository.upsert_new_kakao_webtoon(402, "카카오 회차", status=repository.STATUS_ACTIVE)
    folder = ROOT / "kakao"
    folder.mkdir()
    partial = folder / "untouched.part"
    partial.write_bytes(b"partial")
    item = {"title": "카카오 회차", "on_issue": "N"}
    plan = kp.DownloadPlan(mode="new_folder", to_download=[episode(2), episode(4)], locked=[episode(6)])
    class Client:
        async def list_episodes(self, series_id): return item, [episode(2), episode(4)]
        async def fetch_about(self, series_id): return {}
    attempts = []
    async def download(client, series_id, ep, destination):
        attempts.append(ep.number)
        path = destination / f"{ep.number}.zip"
        path.write_bytes(b"finished")
        repository.set_kakao_webtoon_status(series_id, repository.STATUS_UNSUBSCRIBED)
        return path
    active = lambda: repository.get_kakao_webtoon(402)["status"] == repository.STATUS_ACTIVE
    with patch.object(runner, "_plan_for_run", return_value=plan), patch.object(runner, "series_folder", return_value=folder), \
         patch.object(runner, "download_episode", download), patch.object(runner, "write_series_metadata", nothing), \
         patch.object(runner, "_download_locked_with_rental") as rentals:
        result = await runner.run_download(Client(), series_id=402, title=None, download_root=str(ROOT),
                                           max_episodes=10, should_continue=active, use_rental_tickets=True)
    rentals.assert_not_called()
    assert attempts == [2] and result.downloaded == [2] and result.failed is None and result.stopped_reason
    assert (folder / "2.zip").read_bytes() == b"finished" and partial.read_bytes() == b"partial"
    assert result.nothing_left is False and result.ticket_needed is None

    # Explicit manual run_download without a subscription guard remains supported.
    attempts.clear()
    with patch.object(runner, "_plan_for_run", return_value=kp.DownloadPlan(mode="new_folder", to_download=[episode(2), episode(4)])), \
         patch.object(runner, "series_folder", return_value=folder), patch.object(runner, "download_episode", download), \
         patch.object(runner, "write_series_metadata", nothing):
        result = await runner.run_download(Client(), series_id=402, title=None, download_root=str(ROOT), max_episodes=10)
    assert attempts == [2, 4] and result.stopped_reason is None


async def kakao_checks_before_waitfree_and_each_rental():
    active = {"value": True}
    calls = []
    class Client:
        async def ticket_info(self, series_id):
            active["value"] = False
            return NS(waitfree_ready=True)
        async def use_waitfree_ticket(self, product_id):
            raise AssertionError("unsubscribe during ticket lookup must not consume a ticket")
        async def use_rental_ticket(self, product_id):
            calls.append(product_id)
            return True, "", 2
    locked = [episode(7), episode(8)]
    for ep in locked:
        ep.accessible = False
        ep.is_free = False
        ep.waitfree_blocked = True
    waitfree = episode(6)
    downloaded, items = [], []
    opened, failed = await runner._download_first_locked_with_waitfree(
        Client(), 402, waitfree, ROOT, downloaded, items, None, should_continue=lambda: active["value"])
    assert opened is None and failed is None and downloaded == []

    active["value"] = True
    async def download(client, series_id, ep, folder):
        active["value"] = False
        return folder / f"{ep.number}.zip"
    with patch.object(runner, "download_episode", download):
        used, remaining, failed = await runner._download_locked_with_rental(
            Client(), 402, {"is_waitfree": False}, locked, ROOT, downloaded, items, 10, None,
            should_continue=lambda: active["value"])
    assert calls == [7] and used == [7] and downloaded == [7] and failed is None


async def main():
    job_status.start("download")
    await naver_stops_after_resume()
    await naver_stops_after_current_file_and_rest()
    await kakao_stops_at_title_and_cooldown_boundaries()
    await kakao_finishes_current_file_and_manual_stays_available()
    await kakao_checks_before_waitfree_and_each_rental()
    print("PASS: latest subscription state honored at title/cooldown/file boundaries; current files and manual mode preserved")


asyncio.run(main())
