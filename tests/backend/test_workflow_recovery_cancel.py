"""Persisted batch rest and pending cancellation preserve subscription data."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app import db, download_cooldown, job_status, repository, subscription_scope, workflow_progress
from app.config import get_settings
from app.scheduler import download_job
import app.main as main

db.get_connection()


async def cooldown_recovery():
    for platform, title_id in (("naver", "9881"), ("kakao", "9882")):
        if platform == "naver":
            repository.upsert_new(title_id, "휴식 복구")
        else:
            repository.upsert_new_kakao_webtoon(int(title_id), "휴식 복구", status=repository.STATUS_ACTIVE)
        workflow_progress.begin(platform, title_id, "휴식 복구", 2)

        async def interrupt(seconds):
            assert seconds == 600
            assert download_cooldown.deadline(platform, title_id)
            raise asyncio.CancelledError()

        with patch.object(download_cooldown.asyncio, "sleep", interrupt):
            try:
                await download_cooldown.rest(platform, title_id, 600)
            except asyncio.CancelledError:
                pass
        until = download_cooldown.deadline(platform, title_id)
        assert until and workflow_progress.snapshot()["rest_until"] == until
        repository.enqueue_download(platform, title_id, "휴식 복구")
        repository.update_download_queue(repository.list_download_queue()[0]["id"], state="running")
        calls, waits = [], []
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(seconds):
            waits.append(seconds)
            entered.set()
            await release.wait()

        async def download(platforms, *, only_title):
            workflow_progress.begin(platform, only_title, "휴식 복구", 2)
            await download_cooldown.resume(platform, only_title)
            calls.append(only_title)

        with patch.object(download_job, "_run_download_job_impl", download), \
             patch.object(download_cooldown.asyncio, "sleep", wait):
            download_job.start_queued_downloads(recover=True)
            await asyncio.wait_for(entered.wait(), 2)
            assert not calls and 595 < waits[-1] <= 600
            assert workflow_progress.snapshot()["state"] == "resting"
            await download_job.stop_queued_downloads()
            assert download_cooldown.deadline(platform, title_id) == until
            entered.clear()
            download_job.start_queued_downloads(recover=True)
            await asyncio.wait_for(entered.wait(), 2)
            assert not calls
            release.set()
            await asyncio.wait_for(download_job._queue_worker_task, 2)
        assert calls == [title_id] and download_cooldown.deadline(platform, title_id) is None
        assert repository.list_download_queue() == []

    # Pre-upgrade rows without a deadline still run, without extending a new rest.
    repository.enqueue_download("naver", "9881", "이전 형식")
    calls = []
    async def legacy(platforms, *, only_title):
        await download_cooldown.resume("naver", only_title)
        calls.append(only_title)
    with patch.object(download_job, "_run_download_job_impl", legacy):
        download_job.start_queued_downloads(recover=True)
        await asyncio.wait_for(download_job._queue_worker_task, 2)
    assert calls == ["9881"]
    repository.set_setting("download_cooldown:naver:9881", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    await download_cooldown.resume("naver", "9881")
    assert download_cooldown.deadline("naver", "9881") is None


async def cancellation():
    episodes = [{"episode_no": n, "available": True, "owned": False} for n in (1, 2)]
    for platform, title_id in (("naver", "9881"), ("kakao", "9882")):
        subscription_scope.set_choice(platform, title_id, "selected", episodes, episode_nos=[2])
        repository.enqueue_download(platform, title_id, "보존 검증")
    before = {p: repository.get_setting(f"subscription_scope:{p}:{i}") for p, i in (("naver", "9881"), ("kakao", "9882"))}
    file = Path(get_settings().download_root) / "existing.zip"
    file.write_bytes(b"preserve existing archive")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        job_status.start("manual")
        try:
            status = (await client.get("/api/workflow/status")).json()
            assert status["download_busy"] and len(status["queue"]) == 2
            assert all(row["scope_edit_reason"] for row in status["subscriptions"])
            cancelled = await client.post("/api/workflow/queue/cancel", json={"platform": "naver", "title_id": "9881"})
            assert cancelled.status_code == 200 and cancelled.json()["execution"] == "cancelled"
            assert repository.get("9881").status == repository.STATUS_ACTIVE
            assert repository.get_setting("subscription_scope:naver:9881") == before["naver"]
            assert file.read_bytes() == b"preserve existing archive"
            status = (await client.get("/api/workflow/status")).json()
            queued = next(row for row in status["subscriptions"] if row["platform"] == "kakao")
            assert queued["queue_position"] == 1
            item = repository.list_download_queue()[0]
            repository.update_download_queue(item["id"], state="running")
            failed = await client.post("/api/workflow/queue/cancel", json={"platform": "kakao", "title_id": "9882"})
            assert failed.status_code == 409 and repository.list_download_queue()[0]["state"] == "running"
            assert repository.get_setting("subscription_scope:kakao:9882") == before["kakao"]
            repository.update_download_queue(item["id"], state="queued")
            cancelled = await client.post("/api/workflow/queue/cancel", json={"platform": "kakao", "title_id": "9882"})
            assert cancelled.status_code == 200 and repository.get_kakao_webtoon(9882)["status"] == repository.STATUS_ACTIVE
            missing = await client.post("/api/workflow/queue/cancel", json={"platform": "kakao", "title_id": "9882"})
            assert missing.status_code == 404
        finally:
            job_status.finish("manual", success=True)
    assert repository.list_download_queue() == []


asyncio.run(cooldown_recovery())
asyncio.run(cancellation())
print("Persisted rest survives repeated worker recovery; queued cancellation preserves scopes, subscriptions and files; running cancellation rejected")
