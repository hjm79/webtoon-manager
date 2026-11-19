"""Commit a newly activated subscription and its explicit scope together."""
import json
from contextlib import nullcontext
import uuid

from app.db import read_lock, write_transaction
from app.repository.common import SOURCE_MANUAL, STATUS_ACTIVE, _now
from app.repository.naver_webtoons import _row_to_record
from app.repository.kakao_webtoons import _row_to_kakao_webtoon

_QUEUE_KEY = "workflow_download_queue"


def _queue(conn) -> list[dict]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_QUEUE_KEY,)).fetchone()
    return json.loads(row["value"]) if row else []


def _save_queue(conn, items: list[dict]) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (_QUEUE_KEY, json.dumps(items)))


def _append_queue(conn, platform: str, title_id: str, title: str) -> list[dict]:
    items = _queue(conn)
    if any(row["platform"] == platform and row["title_id"] == str(title_id) for row in items):
        raise ValueError("이미 다운로드 대기열에 등록된 작품입니다.")
    items.append({"id": uuid.uuid4().hex, "platform": platform, "title_id": str(title_id),
                  "title": title, "state": "queued", "enqueued_at": _now()})
    _save_queue(conn, items)
    return items


def list_download_queue() -> list[dict]:
    with read_lock() as conn:
        return _queue(conn)


def enqueue_download(platform: str, title_id: str, title: str) -> list[dict]:
    if platform not in ("naver", "kakao"):
        raise ValueError("지원하지 않는 플랫폼입니다.")
    table = "webtoons" if platform == "naver" else "kakao_webtoons"
    with write_transaction() as conn:
        row = conn.execute(f"SELECT status FROM {table} WHERE title_id = ?", (title_id,)).fetchone()
        if row is None or row["status"] != STATUS_ACTIVE:
            raise ValueError("구독 중인 작품만 다운로드 대기열에 추가할 수 있습니다.")
        return _append_queue(conn, platform, title_id, title)


def update_download_queue(item_id: str | None = None, *, state: str | None = None, recover: bool = False,
                          reason: str | None = None, retry_at: float | None = None, connection=None) -> None:
    with (write_transaction() if connection is None else nullcontext(connection)) as conn:
        items = _queue(conn)
        if recover:
            for item in items:
                if item["state"] == "running":
                    item["state"] = "queued"
        elif state:
            for item in items:
                if item["id"] == item_id:
                    item["state"] = state
                    item["reason"] = reason
                    item["retry_at"] = retry_at
        else:
            items = [item for item in items if item["id"] != item_id]
        _save_queue(conn, items)


def resume_kakao_auth_queue(*, connection=None) -> None:
    with (write_transaction() if connection is None else nullcontext(connection)) as conn:
        items = _queue(conn)
        for item in items:
            if item['platform'] == 'kakao' and item['state'] in ('blocked_auth', 'retry_wait'):
                item.update(state='queued', reason=None, retry_at=None)
        _save_queue(conn, items)


def cancel_queued_download(platform: str, title_id: str) -> bool:
    with write_transaction() as conn:
        items = _queue(conn)
        selected = [row for row in items if row["platform"] == platform and row["title_id"] == str(title_id)]
        if not selected:
            return False
        if any(row["state"] == "running" for row in selected):
            raise ValueError("이미 실행 중인 작업은 대기 취소할 수 없습니다.")
        _save_queue(conn, [row for row in items if row not in selected])
        return True


def set_scoped_subscription_status(platform: str, title_id: str, status: str) -> None:
    if platform not in ("naver", "kakao"):
        raise ValueError("지원하지 않는 플랫폼입니다.")
    table = "webtoons" if platform == "naver" else "kakao_webtoons"
    with write_transaction() as conn:
        items = _queue(conn)
        selected = [row for row in items if row["platform"] == platform and row["title_id"] == str(title_id)]
        if any(row["state"] == "running" for row in selected):
            raise ValueError("다운로드 중인 작품입니다. 실행 완료 후 구독을 해제해주세요.")
        _save_queue(conn, [row for row in items if row not in selected])
        conn.execute(f"UPDATE {table} SET status = ?, updated_at = ? WHERE title_id = ?", (status, _now(), title_id))


def download_subscription_snapshot(platforms: set[str], *, include_queued: bool = False) -> tuple[list, list]:
    with read_lock() as conn:
        naver = conn.execute("SELECT * FROM webtoons WHERE status = ? ORDER BY title", (STATUS_ACTIVE,)).fetchall() if "naver" in platforms else []
        kakao = conn.execute("SELECT * FROM kakao_webtoons WHERE status = ? ORDER BY title", (STATUS_ACTIVE,)).fetchall() if "kakao" in platforms else []
        if not include_queued:
            queued = {(row["platform"], row["title_id"]) for row in _queue(conn)}
            naver = [row for row in naver if ("naver", str(row["title_id"])) not in queued]
            kakao = [row for row in kakao if ("kakao", str(row["title_id"])) not in queued]
    return [_row_to_record(row) for row in naver], [_row_to_kakao_webtoon(row) for row in kakao]


class AlreadySubscribedError(ValueError):
    pass


def activate_scoped_subscription(platform: str, title_id: str, title: str, thumbnail_url: str,
                                 author_summary: str, scope: dict, *, enqueue: bool = False) -> None:
    if platform not in ("naver", "kakao"):
        raise ValueError("지원하지 않는 플랫폼입니다.")
    table = "webtoons" if platform == "naver" else "kakao_webtoons"
    now = _now()
    with write_transaction() as conn:
        row = conn.execute(f"SELECT status FROM {table} WHERE title_id = ?", (title_id,)).fetchone()
        if row is not None and row["status"] == STATUS_ACTIVE:
            raise AlreadySubscribedError("이미 구독 중인 작품입니다. 기존 다운로드 범위는 유지합니다.")
        if platform == "naver":
            conn.execute(
                "INSERT OR IGNORE INTO webtoons "
                "(title_id, title, status, added_source, thumbnail_url, ever_subscribed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (title_id, title, STATUS_ACTIVE, SOURCE_MANUAL, thumbnail_url, now, now),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO kakao_webtoons "
                "(title_id, title, status, thumbnail_url, author_summary, ever_subscribed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (title_id, title, STATUS_ACTIVE, thumbnail_url, author_summary, now, now),
            )
            if author_summary:
                conn.execute("UPDATE kakao_webtoons SET author_summary = ? WHERE title_id = ?", (author_summary, title_id))
        conn.execute(f"UPDATE {table} SET status = ?, ever_subscribed = 1, updated_at = ? WHERE title_id = ?",
                     (STATUS_ACTIVE, now, title_id))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"subscription_scope:{platform}:{title_id}", json.dumps(scope)),
        )
        if enqueue:
            _append_queue(conn, platform, title_id, title)
