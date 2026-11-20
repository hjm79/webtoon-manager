"""
백업/복원, 앱 설정(app_settings) 관련 회귀 테스트.
"""

import pytest

from app import db, services
from conftest import make_chapter_zip


def test_backup_and_restore_roundtrip_preserves_progress(client, library):
    """GIVEN 진행률을 저장해둔 시리즈가 있을 때"""
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "001 1화.zip"))
    make_chapter_zip(str(library / "naver" / "테스트웹툰" / "002 2화.zip"))
    client.post("/api/rescan")
    sid = client.get("/api/series").json()[0]["id"]
    chapters = client.get(f"/api/series/{sid}/chapters").json()["chapters"]
    client.put(f"/api/series/{sid}/progress", json={"chapter_id": chapters[1]["id"], "page_index": 0})

    """WHEN 백업을 받아서 그대로 복원하면"""
    backup = client.get("/api/backup").json()
    r = client.post("/api/restore", json=backup)

    """THEN 복원이 성공하고, 진행률도 그대로 유지된다"""
    assert r.status_code == 200
    cont = client.get(f"/api/series/{sid}/continue").json()
    assert cont["chapter_id"] == chapters[1]["id"]


def test_backup_includes_excluded_folder_list(client, library):
    """GIVEN 제외해둔 폴더가 있을 때"""
    make_chapter_zip(str(library / "naver" / "웹툰A" / "001.zip"))
    client.post("/api/rescan")
    client.post("/api/series-folders/exclude", json={"platform": "naver", "series": "웹툰A"})

    """WHEN 백업을 받으면"""
    backup = client.get("/api/backup").json()

    """THEN app_settings 안에 제외 목록 정보가 포함되어 있다"""
    keys = [item["key"] for item in backup["app_settings"]]
    assert "excluded_series" in keys


def test_settings_get_put_roundtrip(client, library):
    """GIVEN 아무 설정도 없을 때"""
    """WHEN 값을 저장하면"""
    r = client.put("/api/settings/status_filter", json={"value": "unread,reading"})
    assert r.status_code == 200

    """THEN 그대로 다시 조회된다"""
    got = client.get("/api/settings/status_filter").json()
    assert got["value"] == "unread,reading"


def test_platform_filter_setting_persists_across_requests(client, library):
    """GIVEN 플랫폼 필터를 저장했을 때"""
    client.put("/api/settings/platform_filter", json={"value": "naver,kakao"})

    """WHEN 다시 조회하면(새로고침을 흉내)"""
    got = client.get("/api/settings/platform_filter").json()

    """THEN 저장한 값이 그대로 유지된다"""
    assert got["value"] == "naver,kakao"


@pytest.mark.parametrize("modified", [
    {"version": 999},
    {"app_settings": None},
    {"read_chapters": [{}]},
])
def test_rejects_invalid_backup_without_changing_settings(client, modified):
    db.set_setting("sort_mode", "title")
    previous = client.get("/api/backup").json()
    r = client.post("/api/restore", json={**previous, "progress": [], "app_settings": [], **modified})
    assert r.status_code == 400
    assert db.get_setting("sort_mode") == "title"


def test_legacy_backup_without_profile_list_rejects_unowned_rows(client):
    db.set_setting("sort_mode", "title")
    payload = {
        "version": 3,
        "progress": [],
        "app_settings": [],
        "profile_settings": [{"profile_id": "missing", "key": "sort", "value": "title"}],
    }
    assert client.post("/api/restore", json=payload).status_code == 400
    assert db.get_setting("sort_mode") == "title"


def test_current_backup_requires_all_sections_and_rolls_back_late_duplicate(client):
    db.set_setting("sort_mode", "title")
    db.set_progress("series", "chapter", 0, 1)
    db.mark_chapters_read("series", ["chapter"])
    previous = client.get("/api/backup").json()
    incomplete = previous.copy()
    del incomplete["archive_progress_imports"]
    assert client.post("/api/restore", json=incomplete).status_code == 400

    duplicate = {**previous, "progress": [], "app_settings": [], "read_chapters": previous["read_chapters"] * 2}
    assert client.post("/api/restore", json=duplicate).status_code == 400
    current = client.get("/api/backup").json()
    assert all(current[key] == previous[key] for key in ("progress", "app_settings", "read_chapters"))


def test_restore_replies_success_with_warning_after_committed_scan_error(client, monkeypatch):
    async def fail_scan():
        raise RuntimeError("scan error")

    monkeypatch.setattr(services, "scan_restored_library", fail_scan)
    payload = {"version": 1, "progress": [{"series_id": "series", "chapter_id": "chapter"}]}
    response = client.post("/api/restore", json=payload)
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["warnings"]
    assert db.get_progress("series")["chapter_id"] == "chapter"


def test_restore_reports_partial_scan_outcome_for_exact_scan(client, monkeypatch):
    async def partial_scan():
        return services._ScanResult({}, {}, ("archive",))

    monkeypatch.setattr(services, "_scan_all_platforms_impl", partial_scan)
    payload = {"version": 1, "progress": [{"series_id": "restored-series", "chapter_id": "restored-chapter"}]}
    response = client.post("/api/restore", json=payload)
    assert response.status_code == 200 and response.json()["ok"]
    assert any("archive" in warning for warning in response.json()["warnings"])
    assert db.get_progress("restored-series")["chapter_id"] == "restored-chapter"
