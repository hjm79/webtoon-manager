"""Explicit first-download choices without rewriting legacy download history."""
import json
import threading

from app import repository

_lock = threading.RLock()
_PREFIX = "subscription_scope:"


def _key(platform: str, title_id: str | int) -> str:
    return f"{_PREFIX}{platform}:{title_id}"


def get(platform: str, title_id: str | int) -> dict:
    raw = repository.get_setting(_key(platform, title_id))
    if raw is None:
        return {"mode": "all", "latest_count": None, "episode_nos": [], "initial_cutoff": 0,
                "pending_initial": [], "completed_numbers": [], "explicit": False}
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("mode") not in ("all", "latest", "selected", "future"):
        raise ValueError("저장된 다운로드 범위가 올바르지 않습니다. 구독 범위를 다시 확인해주세요.")
    value.setdefault("completed_numbers", [])
    value["explicit"] = True
    return value


def set_choice(platform: str, title_id: str | int, mode: str, episodes: list[dict], *, latest_count: int | None = None,
               episode_nos: list[int] | None = None) -> dict:
    available = sorted({row["episode_no"] for row in episodes if row["available"]})
    cutoff = max((row["episode_no"] for row in episodes), default=0)
    selected = available
    if mode != "all" and not episodes:
        raise ValueError("회차 목록이 비어 있어 안전한 시작 범위를 정할 수 없습니다. 인증·작품 정보를 먼저 확인해주세요.")
    if mode == "latest":
        if latest_count is None or not 1 <= latest_count <= 1000:
            raise ValueError("최근 회차 수는 1~1000 사이여야 합니다.")
        if not available:
            raise ValueError("지금 읽을 수 있는 무료 회차가 없습니다. 로그인·인증을 먼저 확인해주세요.")
        selected = available[-latest_count:]
    elif mode == "selected":
        selected = sorted(set(episode_nos or []))
        if not selected or not set(selected) <= set(available):
            raise ValueError("지금 무료로 읽을 수 있는 회차를 하나 이상 선택해주세요.")
    elif mode == "future":
        selected = []
    elif mode != "all":
        raise ValueError("지원하지 않는 다운로드 범위입니다.")
    owned = {row["episode_no"] for row in episodes if row["owned"]}
    value = {"mode": mode, "latest_count": latest_count if mode == "latest" else None,
             "episode_nos": selected if mode == "selected" else [], "initial_cutoff": cutoff,
             "pending_initial": [n for n in selected if n not in owned], "completed_numbers": sorted(owned), "explicit": True}
    with _lock:
        repository.set_setting(_key(platform, title_id), json.dumps(value))
    return value


def allowed_numbers(platform: str, title_id: str | int, numbers: list[int]) -> set[int] | None:
    """None preserves old subscriptions; explicit scopes retain failed initial episodes."""
    value = get(platform, title_id)
    if value["mode"] == "all":
        return set(numbers) - set(value["completed_numbers"]) if value["explicit"] else None
    pending = set(value["pending_initial"])
    return {n for n in numbers if n in pending or n > value["initial_cutoff"]}


def completed(platform: str, title_id: str | int, number: int) -> None:
    with _lock:
        value = get(platform, title_id)
        if not value["explicit"]:
            return
        if number in value["pending_initial"]:
            value["pending_initial"].remove(number)
        if number not in value["completed_numbers"]:
            value["completed_numbers"].append(number)
            value["completed_numbers"].sort()
        repository.set_setting(_key(platform, title_id), json.dumps(value))
