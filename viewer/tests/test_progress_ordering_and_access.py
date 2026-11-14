import asyncio
import threading

import httpx
import pytest

from app import access_control, catalog, db, profile_progress, profile_time_restrictions, profiles, scan, services
from conftest import make_chapter_zip


def _series(client, library):
    for number in range(2):
        make_chapter_zip(str(library / "naver" / "Progress ordering" / f"{number + 1:03d}.zip"), page_count=2)
    assert client.post("/api/rescan").status_code == 200
    sid = client.get("/api/series").json()[0]["id"]
    chapters = client.get(f"/api/series/{sid}/chapters").json()["chapters"]
    return sid, chapters


def _body(chapter, sequence, **values):
    return {"chapter_id": chapter["id"], "page_index": 0, "completed": False,
            "client_session_id": "reader-session-0001", "client_sequence": sequence, **values}


@pytest.mark.parametrize("as_profile", [False, True])
def test_delayed_older_completion_cannot_overwrite_latest_precise_position(client, library, monkeypatch, as_profile):
    sid, chapters = _series(client, library)
    chapter_paths = [chapter["path"] for chapter in catalog.get_series(sid)["chapters"]]
    profile = None
    if as_profile:
        profile_progress.init_schema()
        profile_time_restrictions.init_schema()
        profiles.init_schema()
        profile = profiles.create_profile("Ordering reader")
        profiles.set_allowed_series(profile["id"], [sid])
        monkeypatch.setattr(access_control, "get_profile", lambda request: profile)
    original_io = services.run_platform_io

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        blocked_once = False

        async def delayed_io(platform, function, *args):
            nonlocal blocked_once
            if function is scan.list_zip_image_names and args == (chapter_paths[-1],) and not blocked_once:
                blocked_once = True
                entered.set()
                await release.wait()
            return await original_io(platform, function, *args)

        monkeypatch.setattr(services, "run_platform_io", delayed_io)
        # Cover precomputation reads the first chapter and must not consume the progress barrier.
        assert len(await asyncio.wait_for(services.run_platform_io(
            "naver", scan.list_zip_image_names, chapter_paths[0]), 2)) == 2
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as api:
            older = asyncio.create_task(api.put(f"/api/series/{sid}/progress", json=_body(
                chapters[-1], 1, page_index=1, completed=True)))
            await asyncio.wait_for(entered.wait(), 2)
            newer = await api.put(f"/api/series/{sid}/progress", json=_body(chapters[0], 2, page_offset=0.4))
            assert newer.status_code == 200
            assert newer.json()["saved"] is True
            release.set()
            response = await older
            assert response.status_code == 200
            assert response.json() == {"ok": True, "saved": False, "reason": "stale_progress"}

    asyncio.run(scenario())
    assert access_control.get_progress(profile, sid) == {
        "chapter_id": chapters[0]["id"], "chapter_index": 0, "page_index": 0, "page_offset": 0.4,
    }
    assert not access_control.get_read_chapter_ids(profile, sid)
    if as_profile:
        assert db.get_progress(sid) is None


def test_per_session_sequence_remains_stale_after_another_session_writes(client, library):
    sid, chapters = _series(client, library)
    url = f"/api/series/{sid}/progress"
    assert client.put(url, json=_body(chapters[1], 10, page_offset=0.2)).json()["saved"]
    other = _body(chapters[0], 1, client_session_id="reader-session-0002", page_offset=0.8)
    assert client.put(url, json=other).json()["saved"]
    before = db.export_backup_data()
    assert client.put(url, json=_body(chapters[1], 9)).json()["saved"] is False
    assert db.export_backup_data() == before
    assert client.put(url, json=_body(chapters[0], 11, page_offset=0.1)).json()["saved"]
    assert db.get_progress(sid)["page_offset"] == 0.1


@pytest.mark.parametrize("page", [-1, 2, 999999, db.PAGE_FINISHED_SENTINEL, True, "1", 1.5])
def test_ordinary_progress_rejects_invalid_page_without_progress_or_read_changes(client, library, page):
    sid, chapters = _series(client, library)
    before = db.export_backup_data()
    response = client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[-1]["id"], "page_index": page, "completed": False,
    })
    assert response.status_code == 422
    assert db.export_backup_data() == before
    assert client.get("/api/series").json()[0]["unread_count"] == 2


@pytest.mark.parametrize("extra", [
    {"client_session_id": "reader-session-0001"}, {"client_sequence": 1},
    {"client_session_id": "short", "client_sequence": 1},
    {"client_session_id": "reader-session-0001", "client_sequence": 0},
    {"client_session_id": "reader-session-0001", "client_sequence": True},
    {"client_session_id": "reader-session-0001", "client_sequence": 2**100},
])
def test_partial_or_invalid_client_sequence_is_rejected(client, library, extra):
    sid, chapters = _series(client, library)
    response = client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[0]["id"], "page_index": 0, "completed": False, **extra,
    })
    assert response.status_code == 422
    assert db.get_progress(sid) is None


def test_manual_unread_invalidates_pending_older_progress(client, library, monkeypatch):
    sid, chapters = _series(client, library)
    target_path = catalog.get_series(sid)["chapters"][-1]["path"]
    original_io = services.run_platform_io

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_io(platform, function, *args):
            if function is scan.list_zip_image_names and args == (target_path,):
                entered.set()
                await release.wait()
            return await original_io(platform, function, *args)

        monkeypatch.setattr(services, "run_platform_io", delayed_io)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as api:
            older = asyncio.create_task(api.put(f"/api/series/{sid}/progress", json=_body(
                chapters[-1], 1, page_index=1, completed=True)))
            await asyncio.wait_for(entered.wait(), 2)
            response = await api.put(f"/api/series/{sid}/read-state", json={"scope": "all", "read": False})
            assert response.status_code == 200
            release.set()
            assert (await older).json()["saved"] is False

    asyncio.run(scenario())
    assert db.get_progress(sid) is None
    assert not db.get_read_chapter_ids(sid)


@pytest.mark.parametrize("operation", ["read-state", "toggle"])
@pytest.mark.parametrize("as_profile", [False, True])
@pytest.mark.parametrize("delay_at", ["catalog", "commit"])
def test_older_manual_read_change_cannot_overwrite_newer_progress(client, library, monkeypatch, operation, as_profile, delay_at):
    sid, chapters = _series(client, library)
    profile = None
    if as_profile:
        profile_progress.init_schema()
        profile_time_restrictions.init_schema()
        profiles.init_schema()
        profile = profiles.create_profile("Manual ordering reader")
        profiles.set_allowed_series(profile["id"], [sid])
        monkeypatch.setattr(access_control, "get_profile", lambda request: profile)
    entered, release = threading.Event(), threading.Event()
    original = db.commit_read_state_intent if delay_at == "commit" else catalog.get_series

    def delayed(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(db if delay_at == "commit" else catalog,
                        "commit_read_state_intent" if delay_at == "commit" else "get_series", delayed)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as api:
            if operation == "read-state":
                request = asyncio.to_thread(client.put, f"/api/series/{sid}/read-state", json={"scope": "all", "read": True})
            else:
                request = asyncio.to_thread(client.post, f"/api/series/{sid}/chapters/{chapters[-1]['id']}/toggle-read")
            older = asyncio.create_task(request)
            assert await asyncio.to_thread(entered.wait, 2)
            newer = await api.put(f"/api/series/{sid}/progress", json=_body(chapters[0], 1, page_offset=0.4))
            assert newer.json()["saved"] is True
            release.set()
            response = await older
            assert response.status_code == 200
            assert response.json()["saved"] is False

    asyncio.run(scenario())
    assert access_control.get_progress(profile, sid) == {
        "chapter_id": chapters[0]["id"], "chapter_index": 0, "page_index": 0, "page_offset": 0.4,
    }
    assert not access_control.get_read_chapter_ids(profile, sid)


def test_profile_time_exception_cannot_be_created_by_progress_or_manual_read_state(client, library, monkeypatch):
    sid, chapters = _series(client, library)
    profile_progress.init_schema()
    profiles.init_schema()
    profile = profiles.create_profile("Restricted reader")
    profiles.set_allowed_series(profile["id"], [sid])
    monkeypatch.setattr(access_control, "get_profile", lambda request: profile)
    allowed = {"now": False}
    monkeypatch.setattr(access_control.profile_time_restrictions, "is_within_allowed_time", lambda _: allowed["now"])
    monkeypatch.setattr(access_control.profile_time_restrictions, "get_time_windows", lambda _: [])
    url = f"/api/series/{sid}/progress"
    first, second = chapters
    pages = lambda chapter: f"/api/chapters/{chapter['id']}/pages"

    assert client.put(url, json=_body(second, 1)).status_code == 403
    assert profile_progress.get_progress(profile["id"], sid) is None
    assert client.put(f"/api/series/{sid}/read-state", json={"scope": "all", "read": True}).status_code == 200
    assert client.get(pages(second)).status_code == 403
    assert client.post(f"/api/series/{sid}/chapters/{second['id']}/toggle-read").status_code == 200
    assert client.get(pages(second) + "/0").status_code == 403

    allowed["now"] = True
    assert client.put(url, json=_body(first, 2, page_offset=0.2)).json()["saved"]
    allowed["now"] = False
    assert client.get(pages(first)).status_code == 200
    assert client.get(pages(first) + "/0").status_code == 200
    assert client.put(url, json=_body(first, 3, page_offset=0.6)).json()["saved"]
    before = profile_progress.get_progress(profile["id"], sid)
    assert client.put(url, json=_body(second, 4)).status_code == 403
    assert profile_progress.get_progress(profile["id"], sid) == before
    assert client.put(f"/api/series/{sid}/read-state", json={
        "scope": "chapter", "read": True, "chapter_id": second["id"],
    }).status_code == 200
    assert client.get(pages(second)).status_code == 403
    assert client.get(pages(second) + "/0").status_code == 403
    assert client.get(pages(first) + "/0").status_code == 200
    assert db.get_progress(sid) is None
    profiles.set_allowed_series(profile["id"], [])
    assert client.get(pages(first) + "/0").status_code == 403
    profiles.delete_profile(profile["id"])
    assert client.get(pages(first)).status_code == 403
