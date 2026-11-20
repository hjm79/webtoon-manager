import pytest
from fastapi.testclient import TestClient

from app import db, main as main_module, profile_progress, profile_time_restrictions, profiles
from conftest import make_chapter_zip


def _series(client, library, *, chapter_count=1, page_count=2):
    for number in range(chapter_count):
        make_chapter_zip(str(library / "naver" / "Completion contract" / f"{number + 1:03d}.zip"),
                         page_count=page_count, width=720, height=5000)
    assert client.post("/api/rescan").status_code == 200
    item = client.get("/api/series").json()[0]
    return item["id"], client.get(f"/api/series/{item['id']}/chapters").json()["chapters"]


@pytest.mark.parametrize("completed", [None, False])
@pytest.mark.parametrize("page_count", [1, 2])
@pytest.mark.parametrize("offset", [0, 0.375])
def test_last_image_precision_does_not_imply_completion(client, library, completed, page_count, offset):
    sid, chapters = _series(client, library, page_count=page_count)
    chapter = chapters[-1]["id"]
    body = {"chapter_id": chapter, "page_index": page_count - 1, "page_offset": offset}
    if completed is not None:
        body["completed"] = completed
    assert client.put(f"/api/series/{sid}/progress", json=body).status_code == 200
    stored = db.get_progress(sid)
    assert stored["page_index"] == page_count - 1
    assert stored.get("page_offset", 0) == offset
    resumed = client.get(f"/api/series/{sid}/continue").json()
    assert resumed["page_index"] == page_count - 1
    assert resumed.get("page_offset", 0) == offset
    assert not db.get_read_chapter_ids(sid)


def test_explicit_false_without_offset_keeps_last_image_unread(client, library):
    sid, chapters = _series(client, library)
    assert client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[-1]["id"], "page_index": 1, "completed": False,
    }).status_code == 200
    assert db.get_progress(sid)["page_index"] == 1
    assert not db.get_read_chapter_ids(sid)


def test_saved_coverage_is_independent_of_cursor_and_resets_on_manual_unread(client, library):
    sid, chapters = _series(client, library, page_count=3)
    chapter_id = chapters[0]["id"]
    manifest_url = f"/api/chapters/{chapter_id}/pages?start_reading=true"
    manifest = client.get(manifest_url).json()
    assert manifest["reading_coverage"] is None
    progress_url = f"/api/series/{sid}/progress"
    position = {"chapter_id": chapter_id, "reading_from_chapter_id": chapter_id, "page_index": 2,
                "completed": False, "read_chapter_ids": []}
    coverage = {"chapter_id": chapter_id, "zip_generation": manifest["zip_generation"],
                "page_index": 1, "page_offset": 0.6}
    assert client.put(progress_url, json={**position, "reading_coverage": [coverage]}).json()["saved"] is True
    assert client.get(f"/api/series/{sid}/continue").json()["page_index"] == 2
    assert client.get(manifest_url).json()["reading_coverage"] == {"page_index": 1, "page_offset": 0.6}
    # A second tab can save its cursor without erasing the farther observed frontier.
    assert client.put(progress_url, json={**position, "page_index": 0,
        "reading_coverage": [{**coverage, "page_index": 0, "page_offset": 0.8}]}).json()["saved"] is True
    assert client.get(manifest_url).json()["reading_coverage"] == {"page_index": 1, "page_offset": 0.6}
    assert client.put(progress_url, json={**position,
        "reading_coverage": [{**coverage, "zip_generation": "0" * 32}]}).status_code == 409
    assert client.put(f"/api/series/{sid}/read-state", json={"scope": "all", "read": False}).status_code == 200
    assert client.get(manifest_url).json()["reading_coverage"] is None


def test_explicit_completion_marks_finished_and_clears_precision(client, library):
    sid, chapters = _series(client, library)
    chapter = chapters[-1]["id"]
    for body in [
        {"chapter_id": chapter, "page_index": 1, "page_offset": 0.375, "completed": False},
        {"chapter_id": chapter, "page_index": 1, "completed": True},
    ]:
        assert client.put(f"/api/series/{sid}/progress", json=body).status_code == 200
    assert db.get_progress(sid)["page_index"] == db.PAGE_FINISHED_SENTINEL
    assert db.get_progress_metadata(sid)["page_offset"] == 0
    assert db.get_read_chapter_ids(sid) == {chapter}


def test_legacy_no_offset_request_keeps_previous_completion_contract(client, library):
    sid, chapters = _series(client, library)
    chapter = chapters[-1]["id"]
    assert client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapter, "page_index": 1,
    }).status_code == 200
    assert db.get_progress(sid)["page_index"] == db.PAGE_FINISHED_SENTINEL
    assert db.get_read_chapter_ids(sid) == {chapter}


def test_scoped_progress_keeps_skipped_chapters_unread_after_restore(client, library):
    sid, chapters = _series(client, library, chapter_count=4)
    first, second, third, fourth = (chapter["id"] for chapter in chapters)
    progress_path = f"/api/series/{sid}/progress"
    chapters_path = f"/api/series/{sid}/chapters"
    archive_path = "/api/lookup/read-state"
    archive_params = {"series": "Completion contract", "platform": "naver"}

    # Opening at chapter three saves a resumable cursor without marking either skipped episode.
    jumped = {"chapter_id": third, "reading_from_chapter_id": third, "page_index": 0, "completed": False}
    assert client.put(progress_path, json=jumped).json()["saved"] is True
    assert client.get("/api/series").json()[0]["unread_count"] == 4
    assert [chapter["has_read"] for chapter in client.get(chapters_path).json()["chapters"]] == [False] * 4
    assert client.get(archive_path, params=archive_params).json()["read_numbers"] == []

    backup = client.get("/api/backup").json()
    assert client.post("/api/restore", json=backup).status_code == 200
    assert client.get(f"/api/series/{sid}/continue").json() == {"chapter_id": third, "page_index": 0}
    assert [chapter["has_read"] for chapter in client.get(chapters_path).json()["chapters"]] == [False] * 4

    # Reaching the end of chapter three marks it read without waiting to scroll into chapter four.
    assert client.put(progress_path, json={**jumped, "page_index": 1, "completed": True}).json()["saved"] is True
    assert client.get(f"/api/series/{sid}/continue").json() == {"chapter_id": fourth, "page_index": 0}
    assert db.get_read_chapter_ids(sid) == {third}

    # Resuming at chapter four and finishing preserves the existing mark and the skipped gap.
    assert client.put(progress_path, json={
        "chapter_id": fourth, "reading_from_chapter_id": fourth, "page_index": 1, "completed": True,
    }).json()["saved"] is True
    assert [chapter["has_read"] for chapter in client.get(chapters_path).json()["chapters"]] == [False, False, True, True]
    assert client.get(archive_path, params=archive_params).json()["read_numbers"] == [3, 4]
    assert db.get_read_chapter_ids(sid).isdisjoint({first, second})


def test_scoped_progress_migrates_existing_legacy_cursor_and_marks_traversed_chapters(client, library):
    sid, chapters = _series(client, library, chapter_count=5)
    first, second, third, fourth, fifth = (chapter["id"] for chapter in chapters)
    db.set_progress(sid, second, 1, 0)
    body = {"chapter_id": fifth, "reading_from_chapter_id": third, "page_index": 0, "completed": False}
    assert client.put(f"/api/series/{sid}/progress", json=body).json()["saved"] is True
    assert db.get_read_chapter_ids(sid) == {first, third, fourth}


def test_browser_progress_never_infers_intervening_reads_and_resume_matches_the_card(client, library):
    sid, chapters = _series(client, library, chapter_count=4)
    first, second, third, fourth = (chapter["id"] for chapter in chapters)
    progress = f"/api/series/{sid}/progress"
    assert client.put(progress, json={"chapter_id": third, "reading_from_chapter_id": first,
        "page_index": 0, "completed": False, "read_chapter_ids": [second]}).json()["saved"] is True
    assert db.get_read_chapter_ids(sid) == {second}
    assert client.put(progress, json={"chapter_id": fourth, "reading_from_chapter_id": first,
        "page_index": 1, "completed": True, "read_chapter_ids": []}).json()["saved"] is True
    assert db.get_read_chapter_ids(sid) == {second, fourth}
    assert client.get(f"/api/series/{sid}/continue").json() == {"chapter_id": first, "page_index": 0}
    assert client.get("/api/series").json()[0]["progress_display"].split("/")[0] == chapters[0]["label"]


@pytest.mark.parametrize("start_index", [1, None])
def test_invalid_scoped_start_leaves_progress_untouched(client, library, start_index):
    sid, chapters = _series(client, library, chapter_count=2)
    response = client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[0]["id"],
        "reading_from_chapter_id": chapters[start_index]["id"] if start_index is not None else "unknown",
        "page_index": 1,
    })
    assert response.status_code == 422
    assert db.get_progress(sid) is None
    assert not db.get_read_chapter_ids(sid)


@pytest.mark.parametrize("chapter_index,page_index", [(0, 1), (1, 0), (1, 99)])
def test_invalid_completion_cannot_write_any_progress_or_read_flags(client, library, chapter_index, page_index):
    sid, chapters = _series(client, library, chapter_count=2)
    before = db.export_backup_data()
    response = client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[chapter_index]["id"], "page_index": page_index, "completed": True,
    })
    assert response.status_code == 422
    assert db.export_backup_data() == before


@pytest.mark.parametrize("completed", [None, 0, 1, "true", "false", [], {}])
def test_completion_requires_an_explicit_boolean(client, library, completed):
    sid, chapters = _series(client, library)
    response = client.put(f"/api/series/{sid}/progress", json={
        "chapter_id": chapters[-1]["id"], "page_index": 1, "completed": completed,
    })
    assert response.status_code == 422
    assert db.get_progress(sid) is None
    assert not db.get_read_chapter_ids(sid)


@pytest.mark.parametrize("completed", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_completion_returns_422_without_writes(client, library, completed):
    sid, chapters = _series(client, library)
    response = client.put(
        f"/api/series/{sid}/progress",
        content=f'{{"chapter_id":"{chapters[-1]["id"]}","page_index":1,"completed":{completed}}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert db.get_progress(sid) is None
    assert not db.get_read_chapter_ids(sid)


def test_profile_last_image_precision_and_completion_remain_separate(library, monkeypatch):
    profile_progress.init_schema()
    profile_time_restrictions.init_schema()
    profiles.init_schema()
    with TestClient(main_module.app) as client:
        sid, chapters = _series(client, library)
        profile = profiles.create_profile("Reader")
        profiles.set_allowed_series(profile["id"], [sid])
        from app.routers import series as routes
        monkeypatch.setattr(routes.access_control, "get_profile", lambda request: profile)
        chapter = chapters[-1]["id"]
        body = {"chapter_id": chapter, "page_index": 1, "page_offset": 0.625, "completed": False}
        assert client.put(f"/api/series/{sid}/progress", json=body).status_code == 200
        assert client.get(f"/api/series/{sid}/continue").json()["page_offset"] == 0.625
        assert not profile_progress.get_read_chapter_ids(profile["id"], sid)
        assert db.get_progress(sid) is None
        body["completed"] = True
        assert client.put(f"/api/series/{sid}/progress", json=body).status_code == 200
        assert profile_progress.get_progress(profile["id"], sid)["page_index"] == db.PAGE_FINISHED_SENTINEL
        assert profile_progress.get_read_chapter_ids(profile["id"], sid) == {chapter}
        assert db.get_progress(sid) is None
