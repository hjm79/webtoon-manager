"""Explicit first-download choices without rewriting legacy download history."""
import json
import threading

from app import repository
from app.repository.scoped_subscription import set_naver_download_record

_lock = threading.RLock()
_PREFIX = "subscription_scope:"


class KakaoRangeReviewRequired(ValueError):
    pass


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
               episode_nos: list[int] | None = None, preserve_existing: bool = False) -> dict:
    with _lock:
        value = make_choice(platform, title_id, mode, episodes, latest_count=latest_count,
                            episode_nos=episode_nos, preserve_existing=preserve_existing)
        repository.set_setting(_key(platform, title_id), json.dumps(value))
    return value


def make_choice(platform: str, title_id: str | int, mode: str, episodes: list[dict], *, latest_count: int | None = None,
                episode_nos: list[int] | None = None, preserve_existing: bool = False) -> dict:
    available = sorted({row["episode_no"] for row in episodes if row["available"]})
    cutoff = max((row["episode_no"] for row in episodes), default=0)
    selected = available
    if mode != "all" and not episodes:
        raise ValueError("회차 목록이 비어 있어 안전한 시작 범위를 정할 수 없습니다. 인증·작품 정보를 먼저 확인해주세요.")
    previous = get(platform, title_id) if preserve_existing else None
    owned = {row["episode_no"] for row in episodes if row["owned"]}
    product_aware_kakao = platform == "kakao" and episodes and all(row.get("product_id") for row in episodes)
    if previous is not None and not product_aware_kakao:
        owned |= set(previous["completed_numbers"])
    if mode == "latest":
        if latest_count is None or not 1 <= latest_count <= 1000:
            raise ValueError("최근 회차 수는 1~1000 사이여야 합니다.")
        if not available:
            raise ValueError("지금 읽을 수 있는 무료 회차가 없습니다. 로그인·인증을 먼저 확인해주세요.")
        selected = available[-latest_count:]
    elif mode == "selected":
        selected = sorted(set(episode_nos or []))
        selectable = set(available)
        if previous is not None:
            previous_products = previous.get("initial_products")
            if previous_products is not None:
                previous_ids = set(previous_products.values())
                selectable |= {row["episode_no"] for row in episodes if row.get("product_id") in previous_ids}
            else:
                selectable |= set(previous["episode_nos"])
            selectable |= owned
        if not selected or not set(selected) <= selectable:
            raise ValueError("지금 무료로 읽을 수 있는 회차를 하나 이상 선택해주세요.")
    elif mode == "future":
        selected = []
    elif mode != "all":
        raise ValueError("지원하지 않는 다운로드 범위입니다.")
    value = {"mode": mode, "latest_count": latest_count if mode == "latest" else None,
             "episode_nos": selected if mode == "selected" else [], "initial_cutoff": cutoff,
             "pending_initial": [n for n in selected if n not in owned], "completed_numbers": sorted(owned), "explicit": True}
    if platform == "naver" and mode == "latest":
        value["initial_episode_nos"] = selected
    if platform == "naver" and previous is not None and previous.get("reopened_numbers"):
        value["reopened_numbers"] = previous["reopened_numbers"]
    if product_aware_kakao and mode != "all":
        products = {row["episode_no"]: row["product_id"] for row in episodes}
        if len(products) != len(episodes) or len(set(products.values())) != len(episodes):
            raise ValueError("카카오페이지 회차 식별자가 중복되어 범위를 저장할 수 없습니다.")
        value["initial_products"] = {str(n): products[n] for n in selected if n in products}
        if mode in ("latest", "future"):
            value["boundary_product"] = products[cutoff]
            value["known_products"] = list(products.values())
    return value


def pending_kakao_numbers(scope: dict, episodes) -> set[int]:
    initial = scope.get("initial_products")
    if initial is None:
        return set(scope["pending_initial"])
    pending_products = {initial.get(str(n)) for n in scope["pending_initial"]}
    return {ep.number for ep in episodes if ep.product_id in pending_products}


def allowed_kakao_numbers(title_id: int, episodes, *, reopen=()) -> set[int] | None:
    scope = get("kakao", title_id)
    if scope["mode"] == "all" and scope["explicit"]:
        return {ep.number for ep in episodes}  # Ownership is resolved from ZIPs and receipts, never scope display numbers.
    initial = scope.get("initial_products")
    if initial is None:
        return allowed_numbers("kakao", title_id, [ep.number for ep in episodes], reopen=reopen)
    pending = pending_kakao_numbers(scope, episodes)
    if scope["mode"] == "selected":
        selected = set(initial.values())
        return pending | {ep.number for ep in episodes if ep.product_id in selected and ep.number in reopen}
    known = set(scope["known_products"])
    boundary = max((ep.number for ep in episodes if ep.product_id in known), default=None)
    if boundary is None:
        raise KakaoRangeReviewRequired("이전 카카오페이지 회차를 현재 목록에서 찾지 못했습니다. 구독 범위를 다시 확인해주세요.")
    return pending | {ep.number for ep in episodes if ep.number > boundary and ep.product_id not in known}


def allowed_numbers(platform: str, title_id: str | int, numbers: list[int], *, reopen=()) -> set[int] | None:
    """None preserves old subscriptions; explicit scopes retain failed initial episodes."""
    value = get(platform, title_id)
    reopen = set(reopen)
    if value["mode"] == "all":
        return set(numbers) - (set(value["completed_numbers"]) - reopen) if value["explicit"] else None
    pending = set(value["pending_initial"])
    if value["mode"] == "selected":
        return set(numbers) & (pending | reopen) & set(value["episode_nos"])
    return {n for n in numbers if n in pending or n > value["initial_cutoff"]}


def completed(platform: str, title_id: str | int, number: int, *, product_id: int | None = None) -> None:
    with _lock:
        value = get(platform, title_id)
        if not value["explicit"]:
            return
        products = value.get("initial_products")
        if products is None:
            finished = number
        else:
            finished = next((int(n) for n, identity in products.items() if identity == product_id), None)
        if finished in value["pending_initial"]:
            value["pending_initial"].remove(finished)
        if number not in value["completed_numbers"]:
            value["completed_numbers"].append(number)
            value["completed_numbers"].sort()
        reopened = value.get("reopened_numbers", [])
        if number in reopened:
            reopened.remove(number)
        repository.set_setting(_key(platform, title_id), json.dumps(value))


def update_naver_receipts(title_id: str, numbers: list[int], downloaded: bool, available: list[int],
                          previous: int, folder_last: int) -> int | None:
    with _lock:
        scope = get("naver", title_id)
        if not scope["explicit"]:
            last = max(previous, numbers[-1]) if downloaded else min(previous, numbers[0] - 1)
            return last if set_naver_download_record(title_id, last, None) else None
        targets = set(numbers)
        pending = set(scope["pending_initial"])
        received = set(scope["completed_numbers"])
        reopened = set(scope.get("reopened_numbers", ()))
        if downloaded:
            pending -= targets
            received |= targets
            reopened -= targets
        else:
            received -= targets
            reopened |= targets
            if scope["mode"] == "selected":
                pending |= targets & set(scope["episode_nos"])
            elif scope["mode"] == "latest":
                initial = scope.get("initial_episode_nos")
                if initial is None:
                    initial = sorted(n for n in available if n <= scope["initial_cutoff"])[-scope["latest_count"]:]
                pending |= targets & set(initial)
        scope.update(pending_initial=sorted(pending), completed_numbers=sorted(received), reopened_numbers=sorted(reopened))
        last = max(received, default=0)
        last = max(last, folder_last)
        return last if set_naver_download_record(title_id, last, scope) else None
