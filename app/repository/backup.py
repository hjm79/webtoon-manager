"""설정 백업 내보내기/검증/복원.
"""

from datetime import datetime, timezone
import json

from app.db import read_lock, write_transaction
from app.repository.scoped_subscription import _QUEUE_KEY, normalize_restored_queue

# ── 백업/복원 ───────────────────────────────────────────────────────

# 백업 파일 형식 버전. 새 버전의 백업은 구버전 프로그램이 복원하지 못하게 막고(데이터가 조용히 잘못 들어가는 것보다 낫다),
# 버전이 없는(예전) 백업은 1로 보고 그대로 복원한다.
BACKUP_FORMAT_VERSION = 2


# 백업에 넣지 않는 설정은 세 종류다. 복원할 때의 취급이 서로 다르다.
#  ① 비밀값: 암호화 키가 이 환경의 데이터 볼륨에만 있어서 다른 환경에서는 어차피 못 풀고, 백업 파일이 새어도 안전하도록 뺀다.
#     복원해도 **지금 쓰는 값을 지우지 않고**(같은 컨테이너에서 복원했다고 웹훅/로그인이 날아가면 안 된다), 백업 파일 안에 들어 있어도 무시한다.
#  ② 캐시: 크고 언제든 다시 채워진다(카카오 전체목록). 복원해도 그대로 둔다.
#  ③ 알림 기록: "하루에 한 번만 알림"을 위한 시각들. 복원하면 초기화해서, 환경이 바뀐 뒤에 다시 알릴 수 있게 한다.
_SECRET_SETTING_KEYS = {"discord_webhook_url", "discord_bot_token", "discord_notify_channel_id", "viewer_scan_token", "kakao_page_cookies",
                        "kakao_page_cookie_version", "kakao_page_cookie_revision", "kakao_page_auth_health", "kakao_page_auto_refresh"}


_LOCAL_SETTING_KEYS = {"kakao_catalog_snapshot", "viewer_scan_state", "report_dispatch_state",
                       "report_list_delivery", "list_alert_retry_at"}


_ALERT_STATE_SETTING_KEYS = {"kakao_page_alert_expired_at", "kakao_page_alert_soon_at", "adult_cookie_expired_notified"}


_NOT_IN_BACKUP_KEYS = _SECRET_SETTING_KEYS | _LOCAL_SETTING_KEYS | _ALERT_STATE_SETTING_KEYS


_BACKUP_TABLES = (
    "webtoons", "settings", "watched_authors", "watched_tags", "kakao_seen_titles", "kakao_webtoons",
    "filename_template_presets", "archive_targets", "archive_history", "episode_history", "archive_pending_finish",
)


def export_all() -> dict:
    """백업에는 디스코드 비밀값을 포함하지 않는다 — 암호화 키가 없는 다른 환경으로
    복원하면 어차피 복호화가 안 되고, 백업 파일 자체가 새어나갈 경우의 위험도 줄인다.
    새 환경에서는 설정 페이지에서 다시 입력하면 된다."""
    with read_lock() as conn:
        settings_rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM settings").fetchall()
            if r["key"] not in _NOT_IN_BACKUP_KEYS
        ]
        return {
            "_meta": {"format_version": BACKUP_FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat()},
            "webtoons": [dict(r) for r in conn.execute("SELECT * FROM webtoons").fetchall()],
            "settings": settings_rows,
            "watched_authors": [dict(r) for r in conn.execute("SELECT * FROM watched_authors").fetchall()],
            "watched_tags": [dict(r) for r in conn.execute("SELECT * FROM watched_tags").fetchall()],
            "kakao_seen_titles": [dict(r) for r in conn.execute("SELECT * FROM kakao_seen_titles").fetchall()],
            "kakao_webtoons": [dict(r) for r in conn.execute("SELECT * FROM kakao_webtoons").fetchall()],
            "filename_template_presets": [dict(r) for r in conn.execute("SELECT * FROM filename_template_presets").fetchall()],
            "archive_targets": [dict(r) for r in conn.execute("SELECT * FROM archive_targets").fetchall()],
            "archive_history": [dict(r) for r in conn.execute("SELECT * FROM archive_history").fetchall()],
            "episode_history": [dict(r) for r in conn.execute("SELECT * FROM episode_history").fetchall()],
            "archive_pending_finish": [dict(r) for r in conn.execute("SELECT * FROM archive_pending_finish").fetchall()],
        }


_WEBTOON_COLUMNS = (
    "title_id", "title", "status", "is_adult", "writer_ids", "added_source",
    "last_downloaded_no", "is_finished", "finish_ack", "thumbnail_url",
    "finish_notified", "genres", "tags", "latest_episode_no", "is_paused",
    "writer_names", "ever_subscribed", "is_new", "has_update", "origin_ids", "origin_names", "new_episode_seen_no", "created_at", "updated_at",
)


_WATCHED_AUTHOR_COLUMNS = ("author_id", "author_name", "enabled", "user_disabled", "platform", "created_at", "updated_at")


_WATCHED_TAG_COLUMNS = ("tag_id", "tag_name", "enabled", "created_at", "updated_at")


_KAKAO_SEEN_TITLE_COLUMNS = ("author_name", "title_id", "title_name", "seen_at")


_KAKAO_WEBTOON_COLUMNS = (
    "title_id", "title", "status", "ever_subscribed", "thumbnail_url", "author_summary", "writer_names", "origin_names",
    "is_finished", "finish_notified", "finish_ack", "downloaded_numbers", "pending_numbers", "waitfree_next_at", "ticket_notified_no", "caught_up_notified_no", "new_episode_seen_no", "created_at", "updated_at",
)


_FILENAME_TEMPLATE_PRESET_COLUMNS = ("id", "name", "template", "created_at", "updated_at")


_ARCHIVE_TARGET_COLUMNS = (
    "title_id", "dest_base_path", "dest_type", "enabled", "source_type", "source_dest_type",
    "source_path", "display_name", "filename_template_preset_id", "created_at", "updated_at",
)


_ARCHIVE_HISTORY_COLUMNS = ("id", "title_id", "title_name", "file_name", "archived_at", "trigger_type")


_ARCHIVE_PENDING_FINISH_COLUMNS = ("title_id", "marked_at")


_EPISODE_HISTORY_COLUMNS = (
    "id", "title_id", "title_name", "episode_no", "subtitle", "status", "error_msg", "downloaded_at", "platform",
)


_TABLE_COLUMNS = {
    "webtoons": _WEBTOON_COLUMNS,
    "watched_authors": _WATCHED_AUTHOR_COLUMNS,
    "watched_tags": _WATCHED_TAG_COLUMNS,
    "kakao_seen_titles": _KAKAO_SEEN_TITLE_COLUMNS,
    "kakao_webtoons": _KAKAO_WEBTOON_COLUMNS,
    "filename_template_presets": _FILENAME_TEMPLATE_PRESET_COLUMNS,
    "archive_targets": _ARCHIVE_TARGET_COLUMNS,
    "archive_history": _ARCHIVE_HISTORY_COLUMNS,
    "episode_history": _EPISODE_HISTORY_COLUMNS,
    "archive_pending_finish": _ARCHIVE_PENDING_FINISH_COLUMNS,
}
_REQUIRED_ROW_FIELDS = {
    "webtoons": ("title_id", "title", "created_at", "updated_at"),
    "settings": ("key", "value"),
    "watched_authors": ("author_id", "created_at", "updated_at"),
    "watched_tags": ("tag_id", "created_at", "updated_at"),
    "kakao_seen_titles": ("author_name", "title_id", "seen_at"),
    "kakao_webtoons": ("title_id", "created_at", "updated_at"),
    "filename_template_presets": ("id", "name", "created_at", "updated_at"),
    "archive_targets": ("title_id", "dest_base_path", "created_at", "updated_at"),
    "archive_history": ("id", "title_id", "title_name", "file_name", "archived_at", "trigger_type"),
    "episode_history": ("id", "title_id", "title_name", "episode_no", "status", "downloaded_at"),
    "archive_pending_finish": ("title_id", "marked_at"),
}


def _insert_validated_rows(conn, table: str, allowed_columns: tuple[str, ...], rows: list[dict]) -> None:
    """Use only known SQL columns; legacy optional fields retain database defaults."""
    for row in rows:
        present_columns = [c for c in allowed_columns if c in row]
        placeholders = ", ".join("?" for _ in present_columns)
        column_list = ", ".join(present_columns)  # allowed_columns에서만 골랐으므로 안전
        conn.execute(
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
            [row[c] for c in present_columns],
        )


def validate_backup(data) -> int:
    """Ensure a current snapshot is complete, and accept recognized legacy subsets."""
    if not isinstance(data, dict):
        raise ValueError("백업 데이터 형식이 올바르지 않습니다 (JSON 객체가 아님).")
    meta = data.get("_meta")
    if "_meta" in data and not isinstance(meta, dict):
        raise ValueError("백업 버전 정보가 올바르지 않습니다.")
    version = meta.get("format_version", 1) if meta is not None else 1
    if type(version) is not int or not 1 <= version <= BACKUP_FORMAT_VERSION:
        raise ValueError("이 백업은 더 새로운 버전의 프로그램에서 만들어져서 복원할 수 없습니다. 프로그램을 최신 버전으로 업데이트한 뒤 다시 시도해 주세요.")
    required_tables = _BACKUP_TABLES if version >= 2 else ("webtoons", "settings")
    missing = [table for table in required_tables if table not in data]
    if missing:
        raise ValueError(f"백업 필수 항목이 빠졌습니다: {', '.join(missing)}")

    setting_keys = set()
    for table in _BACKUP_TABLES:
        if table not in data:
            continue
        if not isinstance(data[table], list):
            raise ValueError(f"백업 항목 형식이 올바르지 않습니다: {table}")
        for row in data[table]:
            if not isinstance(row, dict) or any(key not in row for key in _REQUIRED_ROW_FIELDS[table]):
                raise ValueError(f"백업에 필수 값이 빠진 행이 있습니다: {table}")
            if table == "settings":
                key, value = row["key"], row["value"]
                if not isinstance(key, str) or not key or (value is not None and not isinstance(value, str)):
                    raise ValueError("백업 설정 항목 형식이 올바르지 않습니다.")
                if key in _NOT_IN_BACKUP_KEYS:
                    continue
                if key in setting_keys:
                    raise ValueError("백업에 중복된 설정 항목이 있습니다.")
                setting_keys.add(key)
                if key == _QUEUE_KEY:
                    normalize_restored_queue(value)
                if key.startswith("subscription_scope:"):
                    _validate_subscription_scope(value)
            elif any(row[key] is None for key in _REQUIRED_ROW_FIELDS[table]):
                raise ValueError(f"백업에 필수 값이 비어 있는 행이 있습니다: {table}")
    return version


def _validate_subscription_scope(raw: str | None) -> None:
    try:
        scope = json.loads(raw)
        if (not isinstance(scope, dict) or scope.get("mode") not in ("all", "latest", "selected", "future")
                or type(scope.get("initial_cutoff")) is not int or scope["initial_cutoff"] < 0):
            raise ValueError
        for key in ("episode_nos", "pending_initial", "completed_numbers"):
            numbers = scope.get(key, [] if key == "completed_numbers" else None)
            if not isinstance(numbers, list) or any(type(n) is not int or n <= 0 for n in numbers):
                raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("백업에 올바르지 않은 구독 회차 범위가 있습니다.") from exc


def restore_all(data: dict) -> None:
    """Restore one transaction; a legacy backup leaves absent data in place."""
    version = validate_backup(data)
    keep = sorted(_SECRET_SETTING_KEYS | _LOCAL_SETTING_KEYS)

    with write_transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if version >= 2:
            conn.execute(f"DELETE FROM settings WHERE key NOT IN ({', '.join('?' for _ in keep)})", keep)
        for row in data["settings"]:
            key, value = row["key"], row["value"]
            if key not in _NOT_IN_BACKUP_KEYS:
                if key == _QUEUE_KEY:
                    value = normalize_restored_queue(value)
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        for table, columns in _TABLE_COLUMNS.items():
            if table in data:
                conn.execute(f"DELETE FROM {table}")
                _insert_validated_rows(conn, table, columns, data[table])
