"""Portable snapshots of the viewer's reading and library state."""

import math
import re
import secrets
from datetime import datetime, timezone

from . import access_requests, db, profile_devices, profile_progress, profile_settings, profile_time_restrictions, profiles


BACKUP_VERSION = 4

_TABLE_COLUMNS = {
    "progress": ("series_id", "chapter_id", "chapter_index", "page_index", "updated_at", "page_offset", "explicit_read_state"),
    "app_settings": ("key", "value"),
    "read_chapters": ("series_id", "chapter_id"),
    "archive_progress_imports": ("series_id", "chapter_id", "source_chapter_id"),
    "profiles": ("id", "name", "created_at"),
    "profile_browse_filters": ("profile_id", "platform", "age_rating"),
    "profile_allowed_series": ("profile_id", "series_id"),
    "profile_progress": ("profile_id", "series_id", "chapter_id", "chapter_index", "page_index", "updated_at", "page_offset"),
    "profile_read_chapters": ("profile_id", "series_id", "chapter_id"),
    "profile_settings": ("profile_id", "key", "value"),
    "profile_time_windows": ("profile_id", "day_of_week", "start_minute", "end_minute"),
    "access_requests": ("profile_id", "series_id", "status", "requested_at"),
    "reading_coverage": ("actor_key", "series_id", "chapter_id", "zip_generation", "page_index", "page_offset"),
}
_PROFILE_TABLES = tuple(table for table, columns in _TABLE_COLUMNS.items() if columns[0] == "profile_id")
_LEGACY_DEFAULTS = {"chapter_index": 0, "page_index": 0, "page_offset": 0.0, "explicit_read_state": False}
_SQLITE_INT_MAX = 2**63 - 1
_ZIP_GENERATION = re.compile(r"[0-9a-f]{32}")


def _init_profile_schema() -> None:
    for module in (profiles, profile_progress, profile_settings, profile_time_restrictions, access_requests, profile_devices):
        module.init_schema()


def export_snapshot() -> dict:
    _init_profile_schema()
    with db.db_connection() as conn:
        conn.execute("BEGIN")
        payload = {"version": BACKUP_VERSION, "exported_at": datetime.now(timezone.utc).isoformat()}
        for table, columns in _TABLE_COLUMNS.items():
            query = f"SELECT {', '.join(columns)} FROM {table}"
            if table in _PROFILE_TABLES:
                query += " WHERE profile_id IN (SELECT id FROM profiles)"
            if table == "reading_coverage":
                query += " WHERE actor_key = 'admin' OR actor_key IN (SELECT 'profile:' || id FROM profiles)"
            selected = conn.execute(query).fetchall()
            payload[table] = [dict(zip(columns, row)) for row in selected]
        for row in payload["progress"]:
            row["explicit_read_state"] = bool(row["explicit_read_state"])
        for table in ("progress", "profile_progress"):
            for row in payload[table]:
                row["page_offset"] = db.normalize_page_offset(row["page_offset"])
        return payload


def _valid_value(column: str, value) -> bool:
    if column in ("chapter_index", "page_index"):
        return type(value) is int and 0 <= value <= _SQLITE_INT_MAX
    if column in ("day_of_week", "start_minute", "end_minute"):
        # Retain old stored ranges with their original access semantics. Dropping a
        # range that never matched could make an otherwise restricted profile unrestricted.
        return type(value) is int and -2**63 <= value <= _SQLITE_INT_MAX
    if column == "page_offset":
        return type(value) in (float, int) and 0 <= value <= 1 and math.isfinite(value)
    if column == "explicit_read_state":
        return type(value) is bool
    if column == "status":
        return value in (access_requests.STATUS_PENDING, access_requests.STATUS_REJECTED)
    if column == "actor_key":
        return isinstance(value, str) and (value == "admin" or value.startswith("profile:") and value != "profile:")
    if column == "zip_generation":
        return isinstance(value, str) and _ZIP_GENERATION.fullmatch(value) is not None
    return isinstance(value, str) and (bool(value) or column == "value")


def validate_snapshot(data: dict) -> dict[str, list[tuple]]:
    version = data.get("version", 1)
    if type(version) is not int or not 1 <= version <= BACKUP_VERSION:
        raise ValueError("지원하지 않는 뷰어 백업 버전입니다.")
    required_tables = (table for table in _TABLE_COLUMNS if table != "reading_coverage") if version == BACKUP_VERSION else ("progress",)
    for table in required_tables:
        if table not in data:
            raise ValueError(f"백업 필수 항목이 빠졌습니다 ({table}).")

    rows = {}
    for table, columns in _TABLE_COLUMNS.items():
        if table not in data:
            continue
        if not isinstance(data[table], list):
            raise ValueError(f"백업 항목이 목록 형식이 아닙니다 ({table}).")
        defaults = {}
        if version < BACKUP_VERSION and table in ("progress", "profile_progress"):
            defaults = {**_LEGACY_DEFAULTS, "updated_at": datetime.now(timezone.utc).isoformat()}
        rows[table] = []
        for source in data[table]:
            if not isinstance(source, dict):
                raise ValueError(f"백업 행 형식이 올바르지 않습니다 ({table}).")
            values = tuple(source.get(column, defaults.get(column)) for column in columns)
            for column, value in zip(columns, values):
                if not _valid_value(column, value):
                    raise ValueError(f"백업에 올바르지 않은 값이 있습니다 ({table}.{column}).")
            if table == "reading_coverage" and values[4] >= db.PAGE_FINISHED_SENTINEL:
                raise ValueError("백업에 올바르지 않은 읽은 페이지 위치가 있습니다.")
            rows[table].append(values)
    if "profiles" in rows and len({row[0] for row in rows["profiles"]}) != len(rows["profiles"]):
        raise ValueError("백업에 중복된 공유 프로필이 있습니다.")
    return rows


def _replace_profiles(conn, rows: list[tuple], existing_ids: set[str], restored_ids: set[str]) -> int:
    profiles.delete_profiles(conn, sorted(existing_ids - restored_ids))

    new_profiles = []
    for profile_id, name, created_at in rows:
        if profile_id in existing_ids:
            conn.execute("UPDATE profiles SET name = ?, created_at = ? WHERE id = ?", (name, created_at, profile_id))
        else:
            new_profiles.append((profile_id, name, secrets.token_urlsafe(24), created_at))
    conn.executemany("INSERT INTO profiles (id, name, token, created_at) VALUES (?, ?, ?, ?)", new_profiles)
    return len(new_profiles)


def restore_snapshot(data: dict) -> dict:
    rows = validate_snapshot(data)
    restores_profiles = any(table in rows for table in ("profiles", *_PROFILE_TABLES))
    coverage_has_profiles = any(row[0] != "admin" for row in rows.get("reading_coverage", ()))
    if restores_profiles or coverage_has_profiles:
        _init_profile_schema()
    with db.db_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        new_profiles = ignored_orphan_rows = 0
        if restores_profiles or coverage_has_profiles:
            existing_ids = {row[0] for row in conn.execute("SELECT id FROM profiles")}
            restored_ids = {row[0] for row in rows["profiles"]} if "profiles" in rows else existing_ids
            allowed_actors = {"admin", *(f"profile:{profile_id}" for profile_id in restored_ids)}
            for table in (*_PROFILE_TABLES, "reading_coverage"):
                if table not in rows:
                    continue
                allowed = allowed_actors if table == "reading_coverage" else restored_ids
                valid_rows = [row for row in rows[table] if row[0] in allowed]
                if len(valid_rows) != len(rows[table]):
                    if "profiles" not in rows:
                        raise ValueError("백업에 존재하지 않는 공유 프로필의 데이터가 있습니다.")
                    # Some earlier full snapshots include child rows left by profile deletion.
                    ignored_orphan_rows += len(rows[table]) - len(valid_rows)
                    rows[table] = valid_rows
            if "profiles" in rows:
                new_profiles = _replace_profiles(conn, rows["profiles"], existing_ids, restored_ids)

        conn.execute("DELETE FROM progress_write_intents WHERE actor_key = 'admin'")
        restores_profile_reading = bool({"profiles", "profile_progress", "profile_read_chapters"} & rows.keys())
        if restores_profile_reading:
            conn.execute("DELETE FROM progress_write_intents WHERE actor_key LIKE 'profile:%'")
            conn.execute("DELETE FROM profile_reading_sessions")
        if "reading_coverage" not in rows:
            conn.execute("DELETE FROM reading_coverage WHERE actor_key = 'admin'")
            if {"profile_progress", "profile_read_chapters"} & rows.keys():
                conn.execute("DELETE FROM reading_coverage WHERE actor_key LIKE 'profile:%'")
        for table, values in rows.items():
            if table == "profiles":
                continue
            columns = _TABLE_COLUMNS[table]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(f"DELETE FROM {table}")
            conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)

    return {
        "progress_count": len(rows["progress"]),
        "settings_count": len(rows.get("app_settings", ())),
        "read_chapters_count": len(rows.get("read_chapters", ())),
        "archive_progress_imports_count": len(rows.get("archive_progress_imports", ())),
        "profiles_count": len(rows.get("profiles", ())),
        "new_profile_links_count": new_profiles,
        "ignored_orphan_rows_count": ignored_orphan_rows,
        "profile_progress_count": len(rows.get("profile_progress", ())),
        "profile_settings_count": len(rows.get("profile_settings", ())),
        "profile_time_windows_count": len(rows.get("profile_time_windows", ())),
        "access_requests_count": len(rows.get("access_requests", ())),
        "remembered_devices_count": 0,
        "profile_devices_count": 0,
    }
