"""Unified search, explicit subscription admission and real scoped automatic planning."""
import asyncio
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from app import db, repository, subscription_scope, workflow_progress, cookie_health, naver_api, job_status
from app.config import get_settings
from app.api import workflow, webtoons
from app.kakao_page_download import runner as kr
from app.kakao_page_download.models import Episode, ExistingFile
from app.scheduler import naver_download as nd, download_job as dj, kakao_download as kd
from app.api import kakao_download as ka
from app import zipper
from app.folder_scanner import find_downloaded_episode_numbers
import app.main as main

db.get_connection()
rows = [{"episode_no": n, "subtitle": f"{n}화", "available": True, "owned": False} for n in range(1, 6)]


def scope_rules():
    assert subscription_scope.allowed_numbers("naver", "99", [1, 2]) is None
    value = subscription_scope.set_choice("naver", "1", "latest", rows, latest_count=2)
    assert value["initial_cutoff"] == 5 and value["pending_initial"] == [4, 5]
    assert subscription_scope.allowed_numbers("naver", "1", [1, 4, 5, 6]) == {4, 5, 6}
    subscription_scope.completed("naver", "1", 4)
    assert subscription_scope.get("naver", "1")["pending_initial"] == [5]
    assert subscription_scope.allowed_numbers("naver", "1", [1, 4, 5, 6]) == {5, 6}
    subscription_scope.set_choice("naver", "2", "future", rows)
    assert subscription_scope.allowed_numbers("naver", "2", [1, 5, 6]) == {6}
    for mode in ("latest", "future"):
        try:
            subscription_scope.set_choice("naver", "3", mode, [], latest_count=10)
        except ValueError:
            pass
        else:
            raise AssertionError("Empty unauthenticated baseline accepted")
    locked = [{**row, "available": False} for row in rows]
    subscription_scope.set_choice("naver", "3", "future", locked)
    assert subscription_scope.allowed_numbers("naver", "3", [1, 5, 6]) == {6}
    for args in ({"mode": "latest", "latest_count": 0}, {"mode": "selected", "episode_nos": [999]}, {"mode": "selected"}):
        try:
            subscription_scope.set_choice("naver", "invalid", episodes=rows, **args)
        except ValueError:
            pass
        else:
            raise AssertionError(args)
    assert repository.get_setting("subscription_scope:naver:invalid") is None
    subscription_scope.set_choice("naver", "free-hole", "all", [rows[0], {**rows[1], "available": False}, rows[2]])
    subscription_scope.completed("naver", "free-hole", 1)
    subscription_scope.completed("naver", "free-hole", 3)
    assert subscription_scope.allowed_numbers("naver", "free-hole", [1, 2, 3]) == {2}
    subscription_scope.set_choice("naver", "fixed-selection", "selected", rows, episode_nos=[2, 4])
    assert subscription_scope.allowed_numbers("naver", "fixed-selection", [1, 2, 3, 4, 5, 6]) == {2, 4}
    subscription_scope.completed("naver", "fixed-selection", 2)
    assert subscription_scope.allowed_numbers("naver", "fixed-selection", [1, 2, 3, 4, 5, 6]) == {4}
    folder = Path(get_settings().download_root) / "숫자 검증"
    folder.mkdir()
    (folder / "011 숫자 검증 11화.zip").write_bytes(b"test")
    assert find_downloaded_episode_numbers(folder, "숫자 검증", [NS(episode_no=1, subtitle="1화"), NS(episode_no=11, subtitle="11화")]) == {11}


async def automatic_retry():
    repository.upsert_new("101", "선택 범위 검증")
    subscription_scope.set_choice("naver", "101", "latest", rows, latest_count=2)
    settings = get_settings()
    episodes = [NS(episode_no=n, subtitle=f"{n}화") for n in range(1, 6)]
    attempts = []
    fail = {5}
    async def title_info(*_):
        return NS(title_name="선택 범위 검증", is_adult=False, webtoon_type="webtoon")
    async def all_episodes(*_):
        return episodes
    async def one(**kwargs):
        no = kwargs["episode"].episode_no
        attempts.append(no)
        return no not in fail, None
    async def no_sleep(_):
        return None
    with patch.object(naver_api, "fetch_title_info", title_info), patch.object(naver_api, "fetch_all_episodes", all_episodes), \
         patch.object(naver_api, "free_episodes_only", lambda eps: eps), patch.object(nd, "download_single_episode", one), \
         patch.object(nd, "find_last_downloaded_episode_no", lambda *_: 0), \
         patch.object(nd.comicinfo, "write_comicinfo_file", lambda *_: None), \
         patch.object(nd.comicinfo, "needs_comicinfo", lambda *_: False), \
         patch.object(nd, "zip_episode_folders", lambda _, **kw: None), patch.object(nd.asyncio, "sleep", no_sleep):
        failures = []
        job_status.start("download")
        await nd._download_new_episodes_for_one(None, settings, "101", cookie_health.AdultFetchTracker(), failures)
        assert attempts == [4, 5] and failures
        assert subscription_scope.get("naver", "101")["pending_initial"] == [5]
        assert repository.get("101").last_downloaded_no == 4
        assert workflow_progress.snapshot()["state"] == "error"
        attempts.clear()
        fail.clear()
        episodes.append(NS(episode_no=6, subtitle="6화"))
        await nd._download_new_episodes_for_one(None, settings, "101", cookie_health.AdultFetchTracker(), [])
        assert attempts == [5, 6], attempts
        assert repository.get("101").last_downloaded_no == 6
        assert subscription_scope.get("naver", "101")["pending_initial"] == []
        assert workflow_progress.snapshot()["completed"] == 2
        repository.upsert_new("102", "선택 범위 검증")
        initial = [{**row, "available": row["episode_no"] != 2} for row in rows[:3]]
        subscription_scope.set_choice("naver", "102", "all", initial)
        attempts.clear()
        episodes[:] = [NS(episode_no=n, subtitle=f"{n}화") for n in (1, 3)]
        await nd._download_new_episodes_for_one(None, settings, "102", cookie_health.AdultFetchTracker(), [])
        assert attempts == [1, 3] and repository.get("102").last_downloaded_no == 3
        attempts.clear()
        episodes.insert(1, NS(episode_no=2, subtitle="2화"))
        await nd._download_new_episodes_for_one(None, settings, "102", cookie_health.AdultFetchTracker(), [])
        assert attempts == [2] and repository.get("102").last_downloaded_no == 3


def kakao_scope():
    episodes = [Episode(n + 1000, f"{n}화", n, f"{n}화", True, True, 1, False) for n in range(1, 6)]
    subscription_scope.set_choice("kakao", "222", "selected", rows, episode_nos=[2, 4])
    with patch.object(kr.kakao_records, "sync_before_planning", lambda *_: {4}):
        existing = [ExistingFile(4, "4화", "0004_4화.zip")]
        plan = kr._plan_for_run(222, {"title": "카카오 검증"}, episodes, existing)
    assert [e.number for e in plan.to_download] == [2]
    assert not plan.locked
    subscription_scope.completed("kakao", "222", 4)
    assert subscription_scope.get("kakao", "222")["pending_initial"] == [2]
    subscription_scope.set_choice("kakao", "223", "future", rows)
    with patch.object(kr.kakao_records, "sync_before_planning", lambda *_: set()):
        plan = kr._plan_for_run(223, {"title": "카카오 검증"}, episodes, [])
    assert plan.to_download == [] and plan.locked == []
    initial = [{**row, "available": row["episode_no"] != 2} for row in rows[:3]]
    subscription_scope.set_choice("kakao", "224", "all", initial)
    subscription_scope.completed("kakao", "224", 1)
    subscription_scope.completed("kakao", "224", 3)
    with patch.object(kr.kakao_records, "sync_before_planning", lambda *_: {1, 3}):
        plan = kr._plan_for_run(224, {"title": "카카오 검증"}, episodes[:3], [ExistingFile(3, "3화", "0003_3화.zip")])
    assert [ep.number for ep in plan.to_download] == [2]


async def kakao_preview_records():
    title_id = 90123411
    title = "카카오 회차 조회 검증"
    episodes = [Episode(n + 2000, f"{n}화", n, f"{n}화", n != 3, n != 3, 1, n == 4)
                for n in range(1, 5)]

    async def listing(self, requested_id):
        assert requested_id == title_id
        return {"title": title}, episodes

    async def check(client, expected_owned):
        response = await client.post("/api/workflow/preview", json={"platform": "kakao", "title_id": str(title_id)})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["title"] == title and result["available_count"] == 2
        assert {row["episode_no"] for row in result["episodes"] if row["owned"]} == expected_owned
        assert [row["available"] for row in result["episodes"]] == [True, True, False, False]

    with patch.object(workflow.kakao_page_download.KakaoPageClient, "list_episodes", listing):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert repository.get_kakao_webtoon(title_id) is None
            assert repository.get_kakao_downloaded_numbers(title_id) is None
            await check(client, set())
            assert repository.get_kakao_webtoon(title_id) is None

            repository.upsert_new_kakao_webtoon(title_id, title)
            await check(client, set())
            assert repository.get_kakao_downloaded_numbers(title_id) is None

            repository.set_kakao_downloaded_numbers(title_id, set())
            await check(client, set())
            assert repository.get_kakao_downloaded_numbers(title_id) == set()

            folder = workflow.kakao_page_download.series_folder(workflow.download_roots.kakao_root(get_settings()), title)
            folder.mkdir(parents=True)
            with zipfile.ZipFile(folder / "0002_2화.zip", "w") as archive:
                archive.writestr("0001.jpg", b"test-image")
            repository.set_kakao_downloaded_numbers(title_id, {3})
            await check(client, {2, 3})
            assert repository.get_kakao_downloaded_numbers(title_id) == {3}


async def api_contract():
    async def hidden(*_):
        return {"444"}
    async def naver_search(*_):
        return [NS(title_id="333", title_name="휴재작", thumbnail_url="", is_paused=True, is_finished=False),
                NS(title_id="444", title_name="매일+ 휴재작", thumbnail_url="", is_paused=False, is_finished=False)]
    async def kakao_failure(*_):
        raise RuntimeError("Cookie: should-never-leak")
    async def preview(platform, title_id):
        return {"platform": platform, "title_id": title_id, "title": "새 구독", "episodes": rows, "available_count": 5}
    next_at = datetime.now(timezone.utc) + timedelta(hours=1)
    main.app.state.scheduler = NS(get_jobs=lambda: [NS(id="download_job:0", next_run_time=next_at)])
    request = NS(app=main.app)
    assert workflow.next_run_at(request, "naver") == next_at.isoformat()
    assert workflow.next_run_at(request, "kakao") is None
    with patch.object(workflow.naver_api, "search_webtoons", naver_search), \
         patch.object(workflow.daily_plus, "current_ids", hidden), \
         patch.object(workflow.kakao_api, "search_series", kakao_failure), \
         patch.object(workflow.kakao_catalog, "snapshot", lambda: ([], 0)), \
         patch.object(workflow, "_preview", preview), patch.object(webtoons, "_trigger_enrich", lambda *a, **kw: None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get("/api/workflow/search", params={"query": "휴재"})
            assert response.status_code == 200
            result = response.json()
            assert any(r["title_id"] == "333" for r in result["results"])
            assert all(r["title_id"] != "444" for r in result["results"])
            assert result["errors"][0]["platform"] == "kakao" and "should-never-leak" not in response.text
            payload = {"platform": "naver", "title_id": "333", "title": "새 구독", "scope": {"mode": "latest", "latest_count": 2}}
            before_tasks = len(dj._single_title_tasks)
            response = await client.post("/api/workflow/subscribe", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["execution"] == "scheduled" and response.json()["next_run_at"] == next_at.isoformat()
            assert len(dj._single_title_tasks) == before_tasks
            assert repository.get("333").last_downloaded_no == 0
            assert subscription_scope.get("naver", "333")["pending_initial"] == [4, 5]
            previous = subscription_scope.get("naver", "333")
            response = await client.post("/api/workflow/subscribe", json=payload)
            assert response.status_code == 409 and subscription_scope.get("naver", "333") == previous
            dj._download_claimed_platforms.add("naver")
            try:
                payload["title_id"] = "555"
                payload["start_now"] = True
                response = await client.post("/api/workflow/subscribe", json=payload)
                assert response.status_code == 200 and response.json()["execution"] == "queued"
                assert repository.get("555") is not None
                assert subscription_scope.get("naver", "555")["pending_initial"] == [4, 5]
                assert repository.list_download_queue()[0]["title_id"] == "555"
                await dj.stop_queued_downloads()
                repository.update_download_queue(repository.list_download_queue()[0]["id"])
            finally:
                dj._download_claimed_platforms.clear()
            folder = Path(get_settings().download_root) / "새 구독"
            folder.mkdir(exist_ok=True)
            (folder / "001_검증.zip").write_bytes(b"test")
            response = await client.get("/api/workflow/status")
            assert response.status_code == 200
            row = next(r for r in response.json()["subscriptions"] if r["title_id"] == "333")
            assert row["downloaded_count"] == 1 and row["last_downloaded_no"] == 0
            assert "should-never-leak" not in response.text
    del main.app.state.scheduler


async def one_title_admission():
    calls = []
    async def fake_impl(platforms, scheduled=False, partial=False, only_title=None):
        calls.append((platforms, only_title))
    with patch.object(dj, "_run_download_job_impl", fake_impl):
        dj.reserve_title_download("naver")
        dj.enqueue_reserved_title("naver", "333", "새 구독")
        try:
            dj.reserve_title_download("naver")
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate immediate download accepted")
        await asyncio.gather(*dj._single_title_tasks)
    assert calls == [({"naver"}, "333")]
    assert not dj._download_claimed_platforms and not dj._single_title_tasks


async def archive_failure():
    repository.upsert_new("303", "ZIP 실패 검증")
    subscription_scope.set_choice("naver", "303", "selected", rows, episode_nos=[5])
    settings = get_settings()
    folder = Path(settings.download_root) / "ZIP 실패 검증"
    folder.mkdir()
    stale = folder / "[0004] 남아 있는 폴더"
    stale.mkdir()
    (stale / "0001.jpg").write_bytes(b"stale")
    async def info(*_):
        return NS(title_name="ZIP 실패 검증", is_adult=False, webtoon_type="webtoon")
    async def episodes(*_):
        return [NS(episode_no=5, subtitle="5화")]
    async def single(**_):
        episode = folder / "[0005] 5화"
        episode.mkdir(exist_ok=True)
        (episode / "0001.jpg").write_bytes(b"image")
        return True, episode
    def broken(*_, **__):
        raise OSError("disk full")
    async def no_sleep(_):
        return None
    with patch.object(naver_api, "fetch_title_info", info), patch.object(naver_api, "fetch_all_episodes", episodes), \
         patch.object(naver_api, "free_episodes_only", lambda eps: eps), patch.object(nd, "download_single_episode", single), \
         patch.object(nd, "find_last_downloaded_episode_no", lambda *_: 0), \
         patch.object(nd.comicinfo, "write_comicinfo_file", lambda *_: None), \
         patch.object(nd.comicinfo, "needs_comicinfo", lambda *_: False), patch.object(nd.asyncio, "sleep", no_sleep):
        failures = []
        with patch.object(zipper.zipfile, "ZipFile", broken):
            await nd._download_new_episodes_for_one(None, settings, "303", cookie_health.AdultFetchTracker(), failures)
        assert failures and repository.get("303").last_downloaded_no == 0
        assert subscription_scope.get("naver", "303")["pending_initial"] == [5]
        assert workflow_progress.snapshot()["completed"] == 0 and workflow_progress.snapshot()["failed"] == 1
        assert not list(folder.glob("*.zip")) and not list(folder.glob("*.part"))
        assert (folder / "[0005] 5화" / "0001.jpg").is_file()
        await nd._download_new_episodes_for_one(None, settings, "303", cookie_health.AdultFetchTracker(), [])
        assert repository.get("303").last_downloaded_no == 5
        assert subscription_scope.get("naver", "303")["pending_initial"] == []
        assert len(list(folder.glob("*.zip"))) == 1 and stale.is_dir()


async def authentication_and_shared_admission():
    async def preview(platform, title_id):
        return {"platform": platform, "title_id": title_id, "title": "카카오 인증 검증", "episodes": rows, "available_count": 5}
    async def expired():
        return False
    async def no_notification(*_):
        return None
    payload = {"platform": "kakao", "title_id": "900", "title": "카카오 인증 검증",
               "scope": {"mode": "latest", "latest_count": 2}, "start_now": True}
    with patch.object(workflow, "_preview", preview), \
         patch.object(kd.kakao_page_download, "persist_refreshed_cookies", lambda _: None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            with patch.object(kd.kakao_page_download, "client_from_saved_cookies", lambda *_: None):
                response = await client.post("/api/workflow/subscribe", json=payload)
                assert response.status_code == 400 and repository.get_kakao_webtoon(900) is None
                assert repository.get_setting("subscription_scope:kakao:900") is None
                failures = []
                try:
                    await kd._download_kakao_subscriptions(get_settings(), failures,
                                                         only=[{"title_id": 900, "title": "카카오 인증 검증"}])
                except kd.kakao_page_auth.KakaoAuthPending:
                    pass
                else:
                    raise AssertionError("Authentication failure did not pause")
                assert failures and workflow_progress.snapshot()["state"] == "blocked_auth"
            with patch.object(kd.kakao_page_download, "client_from_saved_cookies", lambda *_: NS(check_login=expired)), \
                 patch.object(kd.kakao_page_auth, "load_cookies", lambda: {}), \
                 patch.object(kd.kakao_page_auth, "notify_if_needed", no_notification):
                response = await client.post("/api/workflow/subscribe", json=payload)
                assert response.status_code == 401 and repository.get_kakao_webtoon(900) is None
                assert repository.get_setting("subscription_scope:kakao:900") is None
                failures = []
                try:
                    await kd._download_kakao_subscriptions(get_settings(), failures,
                                                         only=[{"title_id": 900, "title": "카카오 인증 검증"}])
                except kd.kakao_page_auth.KakaoAuthPending:
                    pass
                else:
                    raise AssertionError("Authentication failure did not pause")
                assert failures and workflow_progress.snapshot()["state"] == "blocked_auth"
            with patch.object(ka.kakao_page_auth, "load_cookies", lambda: {"fake": "not-a-real-cookie"}):
                dj.reserve_title_download("naver")
                try:
                    response = await client.post("/api/kakao-manual/run", json={"series_id": 900, "numbers": [1]})
                    assert response.status_code == 409
                finally:
                    dj.release_title_download("naver")
                ready = asyncio.Event()
                async def held_manual(*_):
                    try:
                        await ready.wait()
                    finally:
                        job_status.finish("manual", success=True)
                with patch.object(ka, "_run_kakao_manual_download", held_manual):
                    response = await client.post("/api/kakao-manual/run", json={"series_id": 900, "numbers": [1]})
                    assert response.status_code == 200
                    try:
                        dj.reserve_title_download("naver")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Naver immediate run collided with Kakao manual run")
                    ready.set()
                    await ka._kakao_manual_task


async def restart_status():
    repository.upsert_new("777", "재시작 완료 검증")
    subscription_scope.set_choice("naver", "777", "latest", rows, latest_count=1)
    subscription_scope.completed("naver", "777", 5)
    folder = Path(get_settings().download_root) / "재시작 완료 검증"
    folder.mkdir()
    with zipfile.ZipFile(folder / "005 재시작 완료 검증 5화.zip", "w") as archive:
        archive.writestr("0001.jpg", b"test-image")
    repository.upsert_new("778", "재시작 미실행 검증")
    subscription_scope.set_choice("naver", "778", "latest", rows, latest_count=1)
    with patch.object(workflow_progress, "title_snapshot", lambda *_: None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get("/api/workflow/status")
            by_id = {row["title_id"]: row for row in response.json()["subscriptions"] if row["platform"] == "naver"}
            assert by_id["777"]["status"] == "available" and by_id["777"]["next_run_at"] is None
            assert by_id["777"]["downloaded_count"] == 1 and by_id["777"]["progress"] is None
            assert by_id["778"]["status"] == "idle" and by_id["778"]["next_run_at"] is None
            next_at = datetime.now(timezone.utc) + timedelta(hours=1)
            main.app.state.scheduler = NS(get_jobs=lambda: [NS(id="download_job:0", next_run_time=next_at)])
            try:
                response = await client.get("/api/workflow/status")
                by_id = {row["title_id"]: row for row in response.json()["subscriptions"] if row["platform"] == "naver"}
                assert by_id["777"]["status"] == "available"
                assert by_id["778"]["status"] == "scheduled" and by_id["778"]["next_run_at"] == next_at.isoformat()
            finally:
                del main.app.state.scheduler


async def edit_scope_contract():
    title_id, title = "950001", "기존 범위 변경 검증"
    repository.upsert_new(title_id, title)
    repository.update_last_downloaded_no(title_id, 3)
    for number in (1, 3):
        repository.add_episode_history(title_id, title, number, f"{number}화", "success")
    subscription_scope.set_choice("naver", title_id, "all", rows)
    for number in (1, 3, 99):
        subscription_scope.completed("naver", title_id, number)
    folder = Path(get_settings().download_root) / title
    folder.mkdir()
    for number in (1, 3):
        with zipfile.ZipFile(folder / f"{number:03d} {title} {number}화.zip", "w") as archive:
            archive.writestr("001.jpg", b"existing-image")
    files_before = {p.name: p.read_bytes() for p in folder.iterdir()}
    record_before = repository.get(title_id)
    history_before = repository.list_episode_history(search=title)
    edited_rows = [{**row, "owned": row["episode_no"] in (1, 3)} for row in rows]

    async def listing(platform, requested_id):
        return {"platform": platform, "title_id": requested_id, "title": title,
                "episodes": edited_rows, "available_count": len(edited_rows)}

    payload = {"platform": "naver", "title_id": title_id, "scope": {"mode": "selected", "episode_nos": [2]}}
    with patch.object(workflow, "_preview", listing):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False), base_url="http://test") as client:
            response = await client.post("/api/workflow/preview", json={"platform": "naver", "title_id": title_id})
            assert response.status_code == 200
            assert response.json()["subscription"] == "active"
            assert response.json()["current_scope"]["completed_numbers"] == [1, 3, 99]
            tasks_before = len(dj._single_title_tasks)
            save = subscription_scope.set_choice
            workflow_progress.begin("naver", "unrelated-progress", "다른 작품", 1)
            workflow_progress.downloaded()
            workflow_progress.finish()
            unrelated_progress = workflow_progress.title_snapshot("naver", "unrelated-progress")
            workflow_progress.begin("naver", title_id, title, 2)
            workflow_progress.finish()

            def guarded_save(*args, **kwargs):
                assert dj._download_job_lock.locked() and dj.download_busy()
                return save(*args, **kwargs)

            with patch.object(subscription_scope, "set_choice", guarded_save):
                response = await client.post("/api/workflow/scope", json=payload)
            assert response.status_code == 200, response.text
            scope = response.json()["scope"]
            assert scope["episode_nos"] == [2] and scope["pending_initial"] == [2]
            assert scope["completed_numbers"] == [1, 3, 99]
            assert len(dj._single_title_tasks) == tasks_before
            assert not dj.download_busy()
            assert workflow_progress.title_snapshot("naver", title_id) is None
            assert workflow_progress.snapshot()["state"] == "idle"
            assert workflow_progress.title_snapshot("naver", "unrelated-progress") == unrelated_progress
            status = (await client.get("/api/workflow/status")).json()
            target = next(row for row in status["subscriptions"] if row["title_id"] == title_id and row["platform"] == "naver")
            assert target["status"] == "idle" and target["progress"] is None
            workflow_progress.begin("naver", "unrelated-progress", "다른 작품", 1)
            global_before = workflow_progress.snapshot()
            response = await client.post("/api/workflow/scope", json=payload)
            assert response.status_code == 200 and workflow_progress.snapshot() == global_before
            assert repository.get(title_id) == record_before
            assert repository.list_episode_history(search=title) == history_before
            assert {p.name: p.read_bytes() for p in folder.iterdir()} == files_before
            assert subscription_scope.allowed_numbers("naver", title_id, [1, 2, 3, 4, 5, 6]) == {2}

            before = repository.get_setting(f"subscription_scope:naver:{title_id}")
            read_setting = repository.get_setting
            def failed_reserved_read(key):
                if key == f"subscription_scope:naver:{title_id}" and dj._download_claimed_platforms:
                    raise RuntimeError("database read unavailable")
                return read_setting(key)
            with patch.object(repository, "get_setting", failed_reserved_read):
                response = await client.post("/api/workflow/scope", json=payload)
                assert response.status_code == 500 and not dj.download_busy()
            assert repository.get_setting(f"subscription_scope:naver:{title_id}") == before
            for body, expected in (({**payload, "title_id": "950002"}, 404),
                                   ({**payload, "scope": {"mode": "selected", "episode_nos": [1000]}}, 400)):
                response = await client.post("/api/workflow/scope", json=body)
                assert response.status_code == expected, response.text
                assert repository.get_setting(f"subscription_scope:naver:{title_id}") == before
                assert not dj.download_busy()
            repository.set_status(title_id, repository.STATUS_UNSUBSCRIBED)
            response = await client.post("/api/workflow/scope", json=payload)
            assert response.status_code == 409
            repository.set_status(title_id, repository.STATUS_ACTIVE)

            for busy in ("claimed", "job", "manual", "kakao"):
                if busy == "claimed":
                    dj.reserve_title_download("kakao")
                elif busy == "job":
                    await dj._download_job_lock.acquire()
                elif busy == "manual":
                    job_status.start("manual")
                else:
                    await workflow.kakao_page_download.download_lock.acquire()
                try:
                    response = await client.post("/api/workflow/scope", json=payload)
                    assert response.status_code == 409 and repository.get_setting(f"subscription_scope:naver:{title_id}") == before
                finally:
                    if busy == "claimed":
                        dj.release_title_download("kakao")
                    elif busy == "job":
                        dj._download_job_lock.release()
                    elif busy == "manual":
                        job_status.finish("manual", success=True)
                    else:
                        workflow.kakao_page_download.download_lock.release()

            async def race_listing(*args):
                dj.reserve_title_download("kakao")
                return await listing(*args)
            with patch.object(workflow, "_preview", race_listing):
                try:
                    response = await client.post("/api/workflow/scope", json=payload)
                    assert response.status_code == 409 and repository.get_setting(f"subscription_scope:naver:{title_id}") == before
                finally:
                    dj.release_title_download("kakao")

            def broken_queue(*_):
                raise RuntimeError("queue unavailable")
            for owner, name in ((webtoons, "_to_out"), (workflow, "next_run_at")):
                with patch.object(owner, name, broken_queue), patch.object(repository, "enqueue_download") as enqueue:
                    response = await client.post("/api/workflow/scope", json={**payload, "start_now": True})
                    assert response.status_code == 500 and not enqueue.called
                    assert repository.get_setting(f"subscription_scope:naver:{title_id}") == before
                    assert not dj.download_busy()
            with patch.object(repository, "enqueue_download", broken_queue):
                response = await client.post("/api/workflow/scope", json={**payload, "start_now": True})
                assert response.status_code == 500
                assert repository.get_setting(f"subscription_scope:naver:{title_id}") == before
                assert not dj.download_busy()

            calls = []
            async def one(platforms, scheduled=False, partial=False, only_title=None):
                calls.append((platforms, only_title))
            with patch.object(dj, "_run_download_job_impl", one):
                response = await client.post("/api/workflow/scope", json={**payload, "start_now": True})
                assert response.status_code == 200 and response.json()["execution"] == "queued"
                await dj._queue_worker_task
            assert calls == [({"naver"}, title_id)] and not dj.download_busy()

    kakao_id = 950003
    repository.upsert_new_kakao_webtoon(kakao_id, "카카오 기존 범위")
    subscription_scope.set_choice("kakao", kakao_id, "selected", rows, episode_nos=[2, 3])
    subscription_scope.completed("kakao", kakao_id, 3)
    repository.set_kakao_downloaded_numbers(kakao_id, {3})
    kakao_before = repository.get_kakao_webtoon(kakao_id)
    kakao_numbers_before = repository.get_kakao_downloaded_numbers(kakao_id)
    locked_rows = [{**row, "available": row["episode_no"] not in (2, 3, 5), "owned": row["episode_no"] == 3} for row in rows]
    async def kakao_listing(platform, requested_id):
        return {"platform": platform, "title_id": requested_id, "title": "카카오 기존 범위",
                "episodes": locked_rows, "available_count": 2}
    with patch.object(workflow, "_preview", kakao_listing):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            body = {"platform": "kakao", "title_id": str(kakao_id), "scope": {"mode": "selected", "episode_nos": [2, 3, 4]}}
            response = await client.post("/api/workflow/scope", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["scope"]["pending_initial"] == [2, 4]
            assert response.json()["scope"]["completed_numbers"] == [3]
            assert repository.get_kakao_webtoon(kakao_id) == kakao_before
            assert repository.get_kakao_downloaded_numbers(kakao_id) == kakao_numbers_before
            response = await client.post("/api/workflow/scope", json={**body, "scope": {"mode": "selected", "episode_nos": [5]}})
            assert response.status_code == 400
            assert subscription_scope.allowed_numbers("kakao", kakao_id, [1, 2, 3, 4, 5, 6]) == {2, 4}


scope_rules()
asyncio.run(automatic_retry())
kakao_scope()
asyncio.run(kakao_preview_records())
asyncio.run(api_contract())
asyncio.run(one_title_admission())
asyncio.run(archive_failure())
asyncio.run(authentication_and_shared_admission())
asyncio.run(restart_status())
asyncio.run(edit_scope_contract())
workflow_progress.begin("naver", "333", "새 구독", 2)
workflow_progress.resting(60)
assert workflow_progress.snapshot()["state"] == "resting" and workflow_progress.snapshot()["rest_until"]
print("scope retry, paused search, secret safety, busy admission, next run, actual file counts and one-title execution passed")
