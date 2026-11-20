"""
휴대용 백업에서 현재 서버의 로그인 정보는 지키고 공유 프로필 데이터를 복원한다.
"""

import importlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import (
    access_requests,
    auth,
    db,
    profile_progress,
    profile_settings,
    profile_time_restrictions,
    profiles,
)
from conftest import make_chapter_zip


@pytest.fixture
def admin_client_with_profile_data(library, monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("PROFILES_ENABLED", "true")
    importlib.reload(main_module)

    make_chapter_zip(str(library / "naver" / "백업테스트웹툰" / "001.zip"))
    make_chapter_zip(str(library / "naver" / "백업테스트웹툰" / "002.zip"))

    with TestClient(main_module.app) as client:
        password = auth.ensure_admin_password_exists()
        client.post("/api/auth/login", json={"password": password})
        client.post("/api/rescan")
        series_id = client.get("/api/series").json()[0]["id"]
        chapters = client.get(f"/api/series/{series_id}/chapters").json()["chapters"]

        created = client.post("/api/admin/profiles", json={"name": "딸"}).json()
        profile_id = created["id"]
        original_token = created["token"]

        client.put(
            f"/api/admin/profiles/{profile_id}/allowed-series", json={"series_ids": [series_id]}
        )
        client.put(
            f"/api/admin/profiles/{profile_id}/browse-filters",
            json={"filters": [{"platform": "naver", "age_rating": "전체 이용가"}]},
        )
        # 프로필 쪽에서 진행률/읽음기록/설정을 남김
        response = client.put(
            f"/p/{original_token}/api/series/{series_id}/progress",
            json={"chapter_id": chapters[0]["id"], "page_index": 2},
        )
        assert response.status_code == 200
        client.put(
            f"/api/admin/profiles/{profile_id}/time-windows",
            json={"windows": [{"day_of_week": 0, "start_minute": 60, "end_minute": 120}]},
        )
        client.put(
            f"/p/{original_token}/api/settings/sort_mode", json={"value": "title"}
        )
        # 대기중인 요청 내역도 하나 남김(다른 시리즈에 대한 요청이라고 가정 - series_id 재사용해도 무방)
        access_requests.create_request(profile_id, series_id)

        yield {
            "client": client, "profile_id": profile_id, "original_token": original_token,
            "series_id": series_id, "chapter_id": chapters[0]["id"],
        }

    monkeypatch.delenv("PROFILES_ENABLED", raising=False)
    importlib.reload(main_module)


def test_backup_includes_profile_data_without_credentials(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]

    """WHEN 백업을 받으면"""
    backup = client.get("/api/backup").json()

    """THEN 프로필 관련 데이터가 전부 포함되어 있다"""
    assert len(backup["profiles"]) == 1
    assert backup["profiles"][0]["id"] == ctx["profile_id"]
    assert "token" not in backup["profiles"][0]
    assert len(backup["profile_allowed_series"]) == 1
    assert len(backup["profile_browse_filters"]) == 1
    assert len(backup["profile_time_windows"]) == 1
    assert len(backup["profile_progress"]) == 1
    assert len(backup["profile_settings"]) >= 1
    assert len(backup["access_requests"]) == 1


def test_restore_issues_new_profile_link_and_brings_back_related_data(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]

    backup = client.get("/api/backup").json()
    client.delete(f"/api/admin/profiles/{ctx['profile_id']}")
    assert client.get("/api/admin/profiles").json() == []

    """WHEN 그 백업으로 복원하면"""
    r = client.post("/api/restore", json=backup)
    assert r.status_code == 200
    body = r.json()
    assert body["profiles_count"] == 1
    assert body["profile_progress_count"] == 1
    assert body["new_profile_links_count"] == 1
    assert body["warnings"]

    restored = client.get("/api/admin/profiles").json()
    assert len(restored) == 1
    restored_token = restored[0]["token"]
    assert restored_token != ctx["original_token"]
    restored_id = restored[0]["id"]
    assert restored_id == ctx["profile_id"]

    """AND 허용 시리즈/둘러보기 필터/접속 시간대/요청 내역도 그대로 복원된다"""
    assert client.get(f"/api/admin/profiles/{restored_id}/allowed-series").json() == [ctx["series_id"]]
    assert len(client.get(f"/api/admin/profiles/{restored_id}/browse-filters").json()) == 1
    assert len(client.get(f"/api/admin/profiles/{restored_id}/time-windows").json()) == 1

    assert client.get(f"/p/{ctx['original_token']}/api/series").status_code == 404
    continue_info = client.get(f"/p/{restored_token}/api/series/{ctx['series_id']}/continue").json()
    assert continue_info["chapter_id"] == ctx["chapter_id"]

    """AND 검색/정렬 설정도 프로필 쪽에 그대로 복원된다"""
    sort_setting = client.get(f"/p/{restored_token}/api/settings/sort_mode").json()
    assert sort_setting["value"] == "title"


def test_export_import_helpers_work_even_when_tables_were_never_created(library, monkeypatch):
    """GIVEN PROFILES_ENABLED를 켠 적이 한 번도 없어서(관련 테이블이 아예 없을 수 있는 상태)"""
    monkeypatch.delenv("PROFILES_ENABLED", raising=False)

    """WHEN 각 모듈의 export_all을 직접 호출하면(테이블이 없어도 스스로 만들고 진행해야 함)"""
    assert profiles.export_all() == {
        "profiles": [], "profile_browse_filters": [], "profile_allowed_series": [],
    }
    assert profile_progress.export_all() == {"profile_progress": [], "profile_read_chapters": []}
    assert profile_settings.export_all() == []
    assert profile_time_restrictions.export_all() == []
    assert access_requests.export_all() == []


def test_backup_never_exports_admin_or_profile_device_credentials(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    client.get(f"/p/{ctx['original_token']}/api/series")  # 프로필 기기 1대 자동 등록됨

    response = client.get("/api/backup")
    backup = response.json()
    assert response.headers["cache-control"] == "no-store"

    assert {"remembered_devices", "profile_devices", "profile_device_requests"}.isdisjoint(backup)
    assert ctx["original_token"] not in response.text
    assert client.cookies["webtoon_device"] not in response.text
    assert client.cookies["webtoon_profile_device"] not in response.text


def test_restore_keeps_active_devices_and_share_link(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    client.get(f"/p/{ctx['original_token']}/api/series")

    backup = client.get("/api/backup").json()
    admin_device = client.cookies["webtoon_device"]
    profile_device = client.cookies["webtoon_profile_device"]

    """WHEN 그 백업으로 복원하면(같은 내용을 다시 써도 안전해야 함)"""
    r = client.post("/api/restore", json=backup)
    assert r.status_code == 200
    assert r.json()["new_profile_links_count"] == 0
    assert auth.is_device_remembered(admin_device)
    assert client.get("/api/admin/profiles").json()[0]["token"] == ctx["original_token"]
    assert client.get(f"/p/{ctx['original_token']}/api/series").status_code == 200
    assert client.cookies["webtoon_profile_device"] == profile_device


def test_legacy_omissions_preserve_profile_and_rejects_old_credentials(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    original = client.get("/api/backup").json()
    legacy = {"version": 1, "progress": []}
    assert client.post("/api/restore", json=legacy).status_code == 200
    assert client.get("/api/admin/profiles").json()[0]["token"] == ctx["original_token"]
    assert client.get("/api/backup").json()["profile_progress"] == original["profile_progress"]

    legacy["version"] = 3
    legacy["profiles"] = [{**original["profiles"][0], "token": "old-share-token"}]
    legacy["remembered_devices"] = [
        {"device_id": "old-admin-device", "label": "Old", "created_at": "old", "last_seen_at": "old"},
    ]
    assert client.post("/api/restore", json=legacy).status_code == 200
    assert not auth.is_device_remembered("old-admin-device")
    assert client.get("/api/admin/profiles").json()[0]["token"] == ctx["original_token"]
    assert client.get("/api/backup").json()["profile_progress"] == original["profile_progress"]


def test_profile_delete_clears_child_rows_and_backup_roundtrips(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    profile_id = ctx["profile_id"]
    actor_key = f"profile:{profile_id}"
    tables = (
        "profile_browse_filters", "profile_allowed_series", "profile_progress",
        "profile_read_chapters", "profile_settings", "profile_time_windows", "access_requests",
        "profile_devices", "profile_device_requests", "profile_reading_sessions",
    )
    with db.db_connection() as conn, conn:
        conn.execute("INSERT OR REPLACE INTO profile_reading_sessions VALUES (?, ?, ?)",
                     (profile_id, ctx["series_id"], ctx["chapter_id"]))
        conn.execute("INSERT INTO reading_coverage VALUES (?, ?, ?, ?, ?, ?)",
                     (actor_key, ctx["series_id"], ctx["chapter_id"], "f" * 32, 0, 1.0))
    assert client.delete(f"/api/admin/profiles/{profile_id}").status_code == 200
    with db.db_connection() as conn:
        for table in tables:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", (profile_id,)).fetchone()[0] == 0
        for table in ("reading_coverage", "progress_write_intents"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE actor_key = ?", (actor_key,)).fetchone()[0] == 0

    snapshot = client.get("/api/backup").json()
    assert snapshot["profiles"] == [] and snapshot["reading_coverage"] == []
    restored = client.post("/api/restore", json=snapshot)
    assert restored.status_code == 200, restored.text


def test_backup_roundtrip_preserves_coverage_and_legacy_cursor_restore_clears_it(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    profile_actor = f"profile:{ctx['profile_id']}"
    with db.db_connection() as conn, conn:
        conn.executemany("INSERT INTO reading_coverage VALUES (?, ?, ?, ?, ?, ?)",
                         [(actor, ctx["series_id"], ctx["chapter_id"], "f" * 32, 1, 0.5)
                          for actor in ("admin", profile_actor, "profile:deleted")])

    snapshot = client.get("/api/backup").json()
    assert {row["actor_key"] for row in snapshot["reading_coverage"]} == {"admin", profile_actor}
    with db.db_connection() as conn, conn:
        conn.execute("DELETE FROM reading_coverage")
    assert client.post("/api/restore", json=snapshot).status_code == 200
    assert client.get("/api/backup").json()["reading_coverage"] == snapshot["reading_coverage"]
    assert client.post("/api/restore", json={"version": 1, "progress": []}).status_code == 200
    assert {row["actor_key"] for row in client.get("/api/backup").json()["reading_coverage"]} == {profile_actor}


def test_backup_omits_legacy_orphans_and_preserves_legacy_time_restriction(admin_client_with_profile_data):
    ctx = admin_client_with_profile_data
    client = ctx["client"]
    profile_id = ctx["profile_id"]
    legacy_window = {"day_of_week": 0, "start_minute": 120, "end_minute": 60}
    profile_time_restrictions.set_time_windows(profile_id, [legacy_window])
    with db.db_connection() as conn, conn:
        conn.execute("INSERT INTO profile_settings VALUES (?, ?, ?)", ("deleted-id", "sort_mode", "title"))

    snapshot = client.get("/api/backup").json()
    assert all(row["profile_id"] == profile_id for row in snapshot["profile_settings"])
    snapshot["profile_settings"].append({"profile_id": "deleted-id", "key": "sort_mode", "value": "title"})
    restored = client.post("/api/restore", json=snapshot)
    assert restored.status_code == 200, restored.text
    assert restored.json()["ignored_orphan_rows_count"] == 1
    assert restored.json()["warnings"]
    assert profile_time_restrictions.get_time_windows(profile_id) == [legacy_window]
    assert not profile_time_restrictions.is_within_allowed_time(profile_id, datetime(2026, 11, 23, 1, 30))
    assert client.put(f"/api/admin/profiles/{profile_id}/time-windows", json={"windows": [legacy_window]}).status_code == 422
    assert profile_time_restrictions.get_time_windows(profile_id) == [legacy_window]
