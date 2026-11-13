"""Structured progress shared by scheduled and one-title downloads."""
import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_IDLE = {"state": "idle", "platform": None, "title_id": None, "title": None,
         "total": 0, "completed": 0, "failed": 0, "current_episode_no": None,
         "rest_until": None, "error": None}
_progress = dict(_IDLE)
_titles: dict[tuple[str, str], dict] = {}


def update(**values) -> None:
    with _lock:
        _progress.update(values)
        if _progress["platform"] and _progress["title_id"]:
            _titles[(_progress["platform"], str(_progress["title_id"]))] = dict(_progress)


def begin(platform: str, title_id: str | int, title: str, total: int) -> None:
    update(state="running", platform=platform, title_id=str(title_id), title=title,
           total=total, completed=0, failed=0, current_episode_no=None, rest_until=None, error=None)


def episode(number: int) -> None:
    update(state="running", current_episode_no=number, rest_until=None)


def downloaded() -> None:
    with _lock:
        count = _progress["completed"] + 1
    with _lock:
        total = max(count, _progress["total"])
    update(completed=count, total=total)


def fail(message: str) -> None:
    with _lock:
        count = _progress["failed"] + 1
    update(state="error", failed=count, rest_until=None, error=message)


def resting(seconds: float) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    update(state="resting", rest_until=until.isoformat())


def finish() -> None:
    with _lock:
        failed = _progress["failed"]
    update(state="error" if failed else "success", rest_until=None, current_episode_no=None)


def snapshot() -> dict:
    with _lock:
        return dict(_progress)


def title_snapshot(platform: str, title_id: str | int) -> dict | None:
    with _lock:
        value = _titles.get((platform, str(title_id)))
        return dict(value) if value else None


def clear_title(platform: str, title_id: str | int) -> None:
    key = (platform, str(title_id))
    with _lock:
        _titles.pop(key, None)
        if (_progress["platform"], _progress["title_id"]) == key:
            _progress.clear()
            _progress.update(_IDLE)
