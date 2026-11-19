"""웹툰 뷰어 서버(webtoon-server)에 스캔을 시키는 공용 조정 장치 — 다운로드(수동 다운로드 포함)와 아카이빙(자동/수동/일괄 이동)이 새 파일을 만들면
뷰어가 다시 훑게 해서, 뷰어 화면과 리포트의 "바로가기" 링크가 새 파일을 반영하게 한다.

시작 전에 처리할 변경 시각과 스캔 ID를 저장한다. 새 변경은 별도 요청으로 남고, 해당 ID의 성공 결과만 반영 완료로 기록한다.
연결·스캔 실패는 시간을 두고 재시도한다. 재시작 후 같은 ID를 다시 확인할 수 있도록 상태는 DB에 저장한다.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import job_status, repository, viewer_connection, webtoon_server_client
from app.json_setting import JsonSetting
from app.crypto import decrypt, encrypt

log = logging.getLogger(__name__)

ENABLED_KEY = "viewer_scan_enabled"
TOKEN_KEY = "viewer_scan_token"
MAX_WAIT_KEY = "viewer_scan_max_wait_minutes"
DEFAULT_MAX_WAIT_MINUTES = 45
MAX_WAIT_LIMIT_MINUTES = 240
_SERVER_URL_KEY = "webtoon_server_url"


def is_enabled() -> bool:
    return repository.get_setting(ENABLED_KEY) == "1"


def get_token() -> str:
    return decrypt(repository.get_setting(TOKEN_KEY) or "")


def get_max_wait_minutes() -> int:
    raw = repository.get_setting(MAX_WAIT_KEY)
    try:
        return min(MAX_WAIT_LIMIT_MINUTES, max(1, int(raw))) if raw is not None else DEFAULT_MAX_WAIT_MINUTES
    except ValueError:
        return DEFAULT_MAX_WAIT_MINUTES


def set_config(*, enabled: bool, token: str | None = None, max_wait_minutes: int | None = None) -> None:
    """token: None이면 기존 값 유지, 빈 문자열이면 지운다."""
    if token and not token.isascii():
        raise ValueError("연동 토큰은 영문/숫자/기호만 쓸 수 있습니다(HTTP 헤더 값으로 보내기 때문입니다).")
    if max_wait_minutes is not None and not (1 <= max_wait_minutes <= MAX_WAIT_LIMIT_MINUTES):
        raise ValueError(f"스캔 대기 시간은 1~{MAX_WAIT_LIMIT_MINUTES}분이어야 합니다.")
    repository.set_setting(ENABLED_KEY, "1" if enabled else None)
    if token is not None:
        repository.set_setting(TOKEN_KEY, encrypt(token) if token else None)
    if max_wait_minutes is not None:
        repository.set_setting(MAX_WAIT_KEY, str(max_wait_minutes))


def is_ready() -> bool:
    """스캔을 시킬 수 있다: 켜져 있고 뷰어 서버 주소와 토큰이 있다."""
    try:
        return is_enabled() and bool(viewer_connection.internal_base(repository.get_setting(_SERVER_URL_KEY))) and bool(get_token())
    except ValueError:
        return False


# ── 요청/진행 상태 ───────────────────────────────────────────────────

STATE_KEY = "viewer_scan_state"
_MAX_ATTEMPTS = 4
_TICK_LOCK = asyncio.Lock()


def _empty_state() -> dict:
    return {
        "requested_at": None,  # 가장 최근의 아직 수락하지 않은 변경 시각
        "active": None,  # 이미 배정한 변경 범위와 검증 대상 ID
        "done_through": None,  # 성공을 확인한 마지막 변경 시각
        "failed_through": None,
        "failure": None,
        "watermark": None,  # 리포트가 스캔 요청 여부를 확인한 시각
    }


# 상태는 설정에 JSON 한 덩어리로 저장한다 — 아카이빙 쪽 알림(request)과 1분 확인(tick)이 다른 스레드에서 겹쳐도 서로의 갱신을 지우지 않게 JsonSetting이 잠금으로 보호한다.
_STATE = JsonSetting(STATE_KEY, _empty_state)
load_state = _STATE.load
update_state = _STATE.update


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _legacy_pending(state: dict) -> str | None:
    # Earlier versions saved an in-flight start time without an identifiable result.
    return state.get("started_at") if state.get("finished") is False else None


def has_work() -> bool:
    """tick이 할 일이 있다: 스캔이 필요한 변경이 있거나, 우리가 시작한 스캔이 아직 도는 중이다(준비가 안 됐으면 없다)."""
    if not is_ready():
        return False
    state = load_state()
    return bool(state["requested_at"] or state["active"] or _legacy_pending(state))


def request(when: datetime) -> None:
    """when 시각에 뷰어가 훑어야 할 변경(새로 받거나 옮긴 파일)이 생겼다고 기록한다 — 연동이 준비돼 있을 때만."""
    if not is_ready():
        return
    with _STATE.lock:
        state = load_state()
        when_iso = _iso(when)
        active = state["active"] or {}
        latest = max(filter(None, (state["done_through"], active.get("through"), state["requested_at"])), default="")
        if latest < when_iso:
            update_state(requested_at=when_iso)


def note_library_changed(count: int, when: datetime | None = None) -> None:
    """파일을 옮기는 작업(아카이빙/일괄 이동)이 끝났을 때 부른다 — 하나라도 옮겼으면 스캔을 요청한다."""
    if count > 0:
        request(when or datetime.now(timezone.utc))


def note_completed_batch(when: datetime | None) -> None:
    """Queue a completed ZIP batch without changing report delivery timing."""
    if when is None:
        return
    try:
        request(when)
    except Exception as error:
        log.warning("완료한 다운로드 배치의 뷰어 스캔 요청을 기록하지 못했습니다: %s", type(error).__name__)


def set_watermark(when: datetime) -> None:
    update_state(watermark=_iso(when))


def covered_state(when: datetime) -> str:
    """변경이 포함된 스캔의 성공 여부. 요청된 변경은 재시도 중에도 waiting이다."""
    state = load_state()
    when_iso = _iso(when)
    if state["done_through"] and state["done_through"] >= when_iso:
        return "done"
    active = state["active"] or {}
    pending = max(filter(None, (active.get("through"), state["requested_at"], _legacy_pending(state))), default="")
    return "waiting" if pending >= when_iso else "failed"


def _claim(now: datetime) -> dict | None:
    with _STATE.lock:
        state = load_state()
        if state["active"] is not None:
            return state["active"]
        through = max(filter(None, (state["requested_at"], _legacy_pending(state))), default=None)
        if not through:
            return None
        active = {"id": uuid4().hex, "through": through, "attempt": 1, "phase": "start",
                  "phase_at": _iso(now), "retry_at": None}
        # A thread recording a new change after this checkpoint updates requested_at only.
        update_state(active=active, requested_at=None, started_at=None, finished=True)
        return active


def _retain(active: dict, **fields) -> None:
    with _STATE.lock:
        if load_state()["active"] == active:
            update_state(active={**active, **fields})


def _complete(active: dict, reason: str | None = None) -> None:
    with _STATE.lock:
        state = load_state()
        if state["active"] != active:
            return
        if reason is None:
            done = max(state["done_through"] or "", active["through"])
            update_state(active=None, done_through=done, failed_through=None, failure=None)
            job_status.log_line("report", "뷰어 서버 스캔이 끝났습니다")
        else:
            update_state(active=None, failed_through=active["through"], failure=reason)
            job_status.log_line("report", f"뷰어 서버 스캔 결과를 확인하지 못했습니다 ({reason})")


def _retry(active: dict, now: datetime, reason: str, *, same_id: bool = False, phase: str = "start") -> None:
    if active["attempt"] >= _MAX_ATTEMPTS:
        _complete(active, reason)
        return
    delay = timedelta(minutes=2 ** (active["attempt"] - 1))
    _retain(active, id=active["id"] if same_id else uuid4().hex, phase=phase, phase_at=_iso(now),
            attempt=active["attempt"] + 1, retry_at=_iso(now + delay))
    job_status.log_line("report", f"뷰어 서버 스캔을 다시 확인합니다 ({reason}, {delay.total_seconds() / 60:g}분 뒤)")


async def tick(session, now: datetime, timeout_seconds: int) -> None:
    """1분 확인에서 현재 스캔 결과 조회와 새 시작 요청을 각각 최대 한 번 보낸다."""
    if not is_ready() or _TICK_LOCK.locked():
        return
    async with _TICK_LOCK:
        await _tick(session, now.astimezone(timezone.utc), timeout_seconds)


async def _tick(session, now: datetime, timeout_seconds: int) -> None:
    active = _claim(now)
    if active is None or (active["retry_at"] and _iso(now) < active["retry_at"]):
        return
    base_url = viewer_connection.internal_base(repository.get_setting(_SERVER_URL_KEY))
    token = get_token()
    phase = active["phase"]
    if phase != "start":
        status = await webtoon_server_client.scan_status(session, base_url, token, timeout_seconds, scan_id=active["id"])
        identified = status is not None and status.get("scan_id") == active["id"]
        outcome = status.get("outcome") if identified else None
        if outcome == "success":
            _complete(active)
            active = _claim(now)
            if active is not None:
                await _start(session, base_url, token, timeout_seconds, active, now)
            return
        if outcome in ("partial", "failed", "cancelled") or (outcome == "unknown" and phase == "poll"):
            _retry(active, now, outcome)
            return
        if phase == "legacy" and not identified:
            _retry(active, now, "스캔 ID 미지원", same_id=True, phase="legacy")
            return
        if phase == "poll" and status is not None and not identified:
            _retry(active, now, "스캔 ID 확인 불가", same_id=True, phase="legacy")
            return
        if status is None or status["running"] or outcome == "running" or phase == "poll":
            if now - datetime.fromisoformat(active["phase_at"]) >= timedelta(minutes=get_max_wait_minutes()):
                _retry(active, now, "대기 시간 초과", same_id=phase != "poll")
            return
    await _start(session, base_url, token, timeout_seconds, active, now)


async def _start(session, base_url: str, token: str, timeout_seconds: int, active: dict, now: datetime) -> None:
    result = await webtoon_server_client.start_scan(session, base_url, token, timeout_seconds, request_id=active["id"])
    if result == "started":
        job_status.log_line("report", "뷰어 서버 스캔을 시작했습니다")
        _retain(active, phase="poll", phase_at=_iso(now), retry_at=None)
    elif result == "running":
        job_status.log_line("report", "뷰어 서버가 이미 스캔 중이라 그 스캔이 끝난 뒤에 시작합니다")
        _retain(active, phase="foreign", phase_at=_iso(now), retry_at=None)
    elif result == "legacy":
        _retry(active, now, "스캔 ID 미지원", same_id=True, phase="legacy")
    elif result == "unsupported":
        _complete(active, "연동 미지원 또는 인증 실패")
    else:
        # The viewer may have started the scan before the connection broke.
        _retry(active, now, "시작 응답 없음", same_id=True)

