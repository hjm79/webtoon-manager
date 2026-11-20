"""Registration during downloads, persistent FIFO admission and stable batch targets."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from app import db, job_status, repository, subscription_scope
from app.api import workflow, webtoons, kakao_webtoons
from app.scheduler import download_job as dj
from app.scheduler import waitfree_job
import app.main as main

db.get_connection()
rows = [{"episode_no": n, "subtitle": f"{n}화", "available": n != 5, "owned": False} for n in range(1, 6)]


def atomic_registration():
    scope = subscription_scope.make_choice("naver", "9900", "selected", rows, episode_nos=[2, 4])
    with db.write_transaction() as conn:
        conn.execute("CREATE TRIGGER reject_test_scope BEFORE INSERT ON settings "
                     "WHEN NEW.key = 'subscription_scope:naver:9900' BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
    try:
        repository.activate_scoped_subscription("naver", "9900", "실패 원자성", "", "", scope, enqueue=True)
    except Exception:
        pass
    else:
        raise AssertionError("Injected DB write failure was ignored")
    assert repository.get("9900") is None
    assert repository.get_setting("subscription_scope:naver:9900") is None
    assert repository.list_download_queue() == []
    with db.write_transaction() as conn:
        conn.execute("DROP TRIGGER reject_test_scope")


async def concurrent_registration():
    repository.upsert_new("9901", "원래 네이버")
    repository.upsert_new_kakao_webtoon(9902, "원래 카카오")
    entered, release = asyncio.Event(), asyncio.Event()
    runs = []
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    async def listing(platform, title_id):
        return {"platform": platform, "title_id": title_id, "title": f"검증 {title_id}",
                "episodes": rows, "available_count": 4}

    async def naver(session, settings, title_id, tracker, failures):
        runs.append(("naver", title_id, subscription_scope.allowed_numbers("naver", title_id, list(range(1, 7)))))
        if title_id == "9901":
            entered.set()
            await release.wait()
        elif title_id == "9904" and sum(run[1] == "9904" for run in runs) == 1:
            failures.append({"title_name": "검증 9904", "episode_no": 3, "subtitle": "ZIP 저장 실패"})

    async def kakao(settings, failures, *, only=None, skip_waiting=False):
        assert only is not None, "Kakao phase must use the snapshot captured before the Naver phase"
        for row in only:
            title_id = str(row["title_id"])
            runs.append(("kakao", title_id, subscription_scope.allowed_numbers("kakao", title_id, list(range(1, 7)))))

    async def noop(*args):
        return None

    async def enrich(title_id, title):
        return kakao_webtoons.KakaoWebtoonOut(**repository.get_kakao_webtoon(title_id))

    with patch.object(workflow, "_preview", listing), patch.object(workflow, "_require_kakao_login", noop), \
         patch.object(webtoons, "_trigger_enrich", lambda *a, **kw: None), \
         patch.object(kakao_webtoons, "enrich_kakao_subscription", enrich), \
         patch.object(dj, "_download_new_episodes_for_one", naver), \
         patch.object(dj, "_download_kakao_subscriptions", kakao), \
         patch.object(dj.cookie_health, "finalize_and_notify", noop), patch.object(dj.asyncio, "sleep", fast_sleep):
        current = asyncio.create_task(dj.run_download_job("both"))
        await asyncio.wait_for(entered.wait(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            for platform, title_id, numbers, start in (("naver", "9903", [2, 4], False),
                                                       ("naver", "9904", [3], True),
                                                       ("kakao", "9905", [2], True)):
                response = await client.post("/api/workflow/subscribe", json={
                    "platform": platform, "title_id": title_id, "title": f"검증 {title_id}",
                    "scope": {"mode": "selected", "episode_nos": numbers}, "start_now": start})
                assert response.status_code == 200, response.text
                assert response.json()["execution"] == ("queued" if start else "scheduled")
                assert subscription_scope.get(platform, title_id)["episode_nos"] == numbers
            assert [entry["title_id"] for entry in repository.list_download_queue()] == ["9904", "9905"]
            assert [entry[1] for entry in runs] == ["9901"], runs
            response = await client.post("/api/workflow/queue", json={"platform": "naver", "title_id": "9901"})
            assert response.status_code == 409 and "현재 다운로드" in response.text
            original_scope = repository.get_setting("subscription_scope:naver:9901")
            repository.set_status("9901", repository.STATUS_UNSUBSCRIBED)
            for start in (False, True):
                response = await client.post("/api/workflow/subscribe", json={
                    "platform": "naver", "title_id": "9901", "title": "진행 중 재구독",
                    "scope": {"mode": "selected", "episode_nos": [1]}, "start_now": start})
                assert response.status_code == 409 and "현재 다운로드" in response.text
                assert repository.get_setting("subscription_scope:naver:9901") == original_scope
            repository.set_status("9901", repository.STATUS_ACTIVE)
            response = await client.post("/api/workflow/subscribe", json={
                "platform": "naver", "title_id": "9904", "title": "중복",
                "scope": {"mode": "all"}, "start_now": True})
            assert response.status_code == 409
            assert len(repository.list_download_queue()) == 2
            response = await client.post("/api/workflow/queue", json={"platform": "naver", "title_id": "9904"})
            assert response.status_code == 409 and len(repository.list_download_queue()) == 2
            response = await client.post("/api/workflow/scope", json={
                "platform": "naver", "title_id": "9904", "scope": {"mode": "selected", "episode_nos": [1]}})
            assert response.status_code == 409 and "대기열" in response.text
            assert subscription_scope.get("naver", "9904")["episode_nos"] == [3]
            status = (await client.get("/api/workflow/status")).json()
            queued = {row["title_id"]: row for row in status["subscriptions"]}
            assert queued["9904"]["queue_position"] == 1 and queued["9905"]["queue_position"] == 2
            assert queued["9904"]["status"] == "queued" and queued["9903"]["status"] != "queued"

            await dj.stop_queued_downloads()
            first = repository.list_download_queue()[0]
            repository.update_download_queue(first["id"], state="running")
            dj.start_queued_downloads(recover=True)
            assert [row["state"] for row in repository.list_download_queue()] == ["queued", "queued"]
            release.set()
            await asyncio.wait_for(current, 3)
            for _ in range(200):
                retained = repository.list_download_queue()
                if len(retained) == 1 and retained[0]["state"] == "retry_download":
                    break
                await real_sleep(0.01)
            else:
                raise AssertionError("failed item must remain retryable while the next title completes")
            assert retained[0]["id"] == first["id"] and retained[0]["attempts"] == 1
            assert retained[0]["retry_at"] > dj.time.time() and "3화" in retained[0]["reason"]
            assert subscription_scope.get("naver", "9904")["pending_initial"] == [3]
            repository.update_download_queue(first["id"], state="retry_download", retry_at=0)
            await asyncio.wait_for(dj._queue_worker_task, 3)
            assert [entry[:2] for entry in runs] == [("naver", "9901"), ("kakao", "9902"),
                                                     ("naver", "9904"), ("kakao", "9905"),
                                                     ("naver", "9904")], runs
            assert runs[2][2] == {3} and runs[3][2] == {2}, runs
            assert repository.list_download_queue() == [] and not dj.download_busy()
            assert subscription_scope.get("naver", "9903")["episode_nos"] == [2, 4]
            assert job_status.snapshot()["download"]["status"] == "success"

            repository.enqueue_download("kakao", "9905", "검증 9905")
            waitfree_calls = []
            async def waitfree_download(settings, failures, *, only, now):
                assert dj.title_running("kakao", "9902")
                response = await client.post("/api/workflow/queue", json={"platform": "kakao", "title_id": "9902"})
                assert response.status_code == 409 and "현재 다운로드" in response.text
                waitfree_calls.extend(str(row["title_id"]) for row in only)
                return [(row, NS(error=None)) for row in only]
            with patch.object(waitfree_job.waitfree_sync, "is_enabled", lambda: True), \
                 patch.object(waitfree_job.waitfree_sync, "due_webtoons", lambda _: [repository.get_kakao_webtoon(i) for i in (9902, 9905)]), \
                 patch.object(waitfree_job, "_download_kakao_subscriptions", waitfree_download):
                await waitfree_job.run_waitfree_due_job()
            assert waitfree_calls == ["9902"] and not dj.title_running("kakao", "9902")
            repository.update_download_queue(repository.list_download_queue()[0]["id"])

            job_status.start("manual")
            try:
                response = await client.post("/api/workflow/queue", json={"platform": "naver", "title_id": "9903"})
                assert response.status_code == 200
                assert repository.list_download_queue()[0]["title_id"] == "9903"
                naver, kakao_rows = repository.download_subscription_snapshot({"naver", "kakao"})
                assert "9903" not in [row.title_id for row in naver]
                response = await client.post("/api/webtoons/9903/unsubscribe")
                assert response.status_code == 200 and repository.list_download_queue() == []
                assert repository.get("9903").status == repository.STATUS_UNSUBSCRIBED
                assert subscription_scope.get("naver", "9903")["episode_nos"] == [2, 4]
                repository.enqueue_download("kakao", "9905", "검증 9905")
                entry = repository.list_download_queue()[0]
                repository.update_download_queue(entry["id"], state="running")
                response = await client.post("/api/kakao-webtoons/9905/unsubscribe")
                assert response.status_code == 409 and repository.get_kakao_webtoon(9905)["status"] == "active"
                repository.update_download_queue(entry["id"], state="queued")
                response = await client.post("/api/kakao-webtoons/9905/unsubscribe")
                assert response.status_code == 200 and repository.list_download_queue() == []
            finally:
                await dj.stop_queued_downloads()
                job_status.finish("manual", success=True)


async def bounded_retries_and_manual_restart():
    title_id = "9910"
    scope = subscription_scope.make_choice("naver", title_id, "selected", rows, episode_nos=[1, 3])
    repository.activate_scoped_subscription("naver", title_id, "보존할 실패 작업", "", "", scope, enqueue=True)
    item = repository.list_download_queue()[0]
    calls = 0

    async def failure(platforms, *, only_title):
        nonlocal calls
        assert platforms == {"naver"} and only_title == title_id
        calls += 1
        if calls == 1:
            subscription_scope.completed("naver", title_id, 1)
        raise RuntimeError("fixture write failed")

    with patch.object(dj, "_DOWNLOAD_RETRY_DELAYS", (0, 0)), patch.object(dj, "_run_download_job_impl", failure):
        await asyncio.wait_for(dj._run_queued_downloads(), 3)
    retained = repository.list_download_queue()[0]
    assert calls == 3 and retained["attempts"] == 3 and retained["state"] == "failed"
    assert retained["id"] == item["id"] and retained["retry_at"] is None
    assert subscription_scope.get("naver", title_id)["pending_initial"] == [3]
    assert not dj.title_queued("naver", title_id)
    naver, _ = repository.download_subscription_snapshot({"naver"})
    assert title_id in [row.title_id for row in naver]

    async def preview(platform, id):
        return {"platform": platform, "title_id": id, "title": "보존할 실패 작업", "episodes": rows}

    async def success(platforms, *, only_title):
        assert subscription_scope.allowed_numbers("naver", only_title, [1, 3, 4]) == {3, 4}
        subscription_scope.completed("naver", only_title, 3)
        subscription_scope.completed("naver", only_title, 4)

    with patch.object(dj, "_run_download_job_impl", success), patch.object(workflow, "_preview", preview):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            data = (await client.get("/api/workflow/status")).json()
            current = next(row for row in data["subscriptions"] if row["title_id"] == title_id)
            assert current["scope_edit_reason"] is None and "queue_position" not in current
            reply = await client.post("/api/workflow/scope", json={"platform": "naver", "title_id": title_id,
                "scope": {"mode": "selected", "episode_nos": [3, 4]}})
            assert reply.status_code == 200 and repository.list_download_queue() == [retained], reply.text
            reply = await client.post("/api/workflow/queue", json={"platform": "naver", "title_id": title_id})
            assert reply.status_code == 200, reply.text
            assert reply.json()["queue"][0]["id"] == item["id"]
            await asyncio.wait_for(dj._queue_worker_task, 3)
    assert not repository.list_download_queue()
    assert subscription_scope.get("naver", title_id)["completed_numbers"] == [1, 3, 4]

    repository.upsert_new_kakao_webtoon(9911, "실패 후 기다무 재개")
    queued = repository.enqueue_download("kakao", "9911", "실패 후 기다무 재개")[0]
    repository.update_download_queue(queued["id"], state="failed", reason="fixture failure")
    webtoon = repository.get_kakao_webtoon(9911)
    received = []

    async def waitfree_batch(due, now):
        received.extend(due)

    with patch.object(waitfree_job.waitfree_sync, "is_enabled", return_value=True), \
         patch.object(waitfree_job.waitfree_sync, "due_webtoons", return_value=[webtoon]), \
         patch.object(waitfree_job, "_run_waitfree_batch", waitfree_batch):
        await waitfree_job.run_waitfree_due_job()
    assert received == [webtoon] and repository.list_download_queue()[0]["state"] == "failed"


async def scheduled_recovery_resolves_only_the_failed_row():
    title_id = "9913"
    repository.upsert_new(title_id=title_id, title="정기 다운로드 복구")
    [entry] = [row for row in repository.enqueue_download("naver", title_id, "정기 다운로드 복구")
               if row["title_id"] == title_id]
    repository.update_download_queue(entry["id"], state="failed", reason="fixture failure", attempts=3)

    async def recovered(session, settings, selected_title_id, tracker, failures):
        if selected_title_id == title_id:
            dj.workflow_progress.begin("naver", title_id, "정기 다운로드 복구", 1)
            dj.workflow_progress.downloaded()
            dj.workflow_progress.finish()

    with patch.object(dj, "_download_new_episodes_for_one", recovered):
        await dj.run_download_job("naver", scheduled=True)
    assert all(row["id"] != entry["id"] for row in repository.list_download_queue())
    assert any(row["state"] == "failed" and row["title_id"] == "9911" for row in repository.list_download_queue())


atomic_registration()
asyncio.run(concurrent_registration())
asyncio.run(bounded_retries_and_manual_restart())
asyncio.run(scheduled_recovery_resolves_only_the_failed_row())
print("atomic subscription, save-only while busy, stable two-platform snapshot, durable FIFO recovery, deduplication, scope protection and failure continuation passed")
