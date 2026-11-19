"""Encrypted Kakao credentials, versioned updates and persistent authentication health."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from app import crypto, db, discord_config, discord_notify, repository

log = logging.getLogger(__name__)
REQUIRED_COOKIES = ("_kau", "_kpwtkn", "_kahai", "_kawlt", "_kpdid")
OPTIONAL_COOKIES = ("_karmt", "_T_ANO")
SETTING_KEY = "kakao_page_cookies"
_VERSION_KEY = "kakao_page_cookie_version"
_REVISION_KEY = "kakao_page_cookie_revision"
_HEALTH_KEY = "kakao_page_auth_health"
_REFRESH_KEY = "kakao_page_auto_refresh"
REFRESH_INTERVAL_SECONDS = 30 * 60
_ALERT_EXPIRED_KEY = "kakao_page_alert_expired_at"
_ALERT_SOON_KEY = "kakao_page_alert_soon_at"
_ALERT_INTERVAL_SECONDS = 24 * 3600
_EXPIRY_WARNING_DAYS = 5
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
MESSAGES = {
    "healthy": "카카오페이지 로그인 정상",
    "unchecked": "쿠키가 저장되어 있습니다. 실제 로그인 확인이 필요합니다.",
    "reauth_required": "카카오페이지 재인증이 필요합니다. 설정에서 새 쿠키를 다시 등록해주세요.",
    "unknown": "카카오페이지 연결 상태를 확인하지 못했습니다. 대기열은 보존됩니다.",
    "not_configured": "카카오페이지 로그인 쿠키를 등록해주세요.",
    "storage_error": "저장된 카카오페이지 인증 정보를 읽지 못했습니다. 암호화 키와 저장소를 확인해주세요.",
    "credentials_changed": "인증 정보가 교체되었습니다. 새 인증으로 이어받습니다.",
}


class KakaoAuthPending(RuntimeError):
    def __init__(self, state="reauth_required", reason="session_rejected", credential_version=None):
        self.state, self.reason = state, reason
        self.credential_version = credential_version
        super().__init__(MESSAGES[state])


def _get(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _put(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


@contextmanager
def _credential_transaction():
    with db.write_transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def version() -> int:
    return int(repository.get_setting(_VERSION_KEY) or 0)


def validate_cookies(data: list, *, require_all=True) -> list[dict]:
    if isinstance(data, list) and not data and not require_all:
        return []
    if not isinstance(data, list) or not data or len(data) > 256:
        raise ValueError("쿠키 목록(JSON 배열)이 비어 있거나 너무 큽니다.")
    cookies, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("쿠키 항목 형식이 올바르지 않습니다.")
        domain = str(item.get("domain", "")).lower().lstrip(".")
        if domain != "kakao.com" and not domain.endswith(".kakao.com"):
            continue
        name, value = item.get("name"), item.get("value")
        path = item.get("path", "/")
        if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
                or not isinstance(value, str) or (name in REQUIRED_COOKIES and not value) or len(value) > 16384
                or any(ord(c) <= 32 or ord(c) >= 127 or c == ';' for c in value)
                or not isinstance(path, str) or not path.startswith("/") or len(path) > 2048
                or any(ord(c) < 32 for c in path)):
            raise ValueError("쿠키 이름·값·경로가 올바르지 않습니다. 로그인 후 JSON을 다시 복사해주세요.")
        expiry = item.get("expirationDate")
        if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or not 0 <= expiry <= 253402300799):
            raise ValueError("쿠키 만료일이 올바르지 않습니다. Cookie-Editor JSON을 다시 복사해주세요.")
        identity = (name, domain, path)
        if identity in seen:
            raise ValueError("같은 이름·도메인·경로의 쿠키가 중복되어 있습니다.")
        seen.add(identity)
        cookies.append({"name": name, "value": value, "domain": item.get("domain", ""), "path": path,
                        "expirationDate": expiry, "secure": bool(item.get("secure", True)),
                        "hostOnly": bool(item.get("hostOnly", False))})
    missing = [name for name in REQUIRED_COOKIES if name not in {c['name'] for c in cookies}]
    if require_all and missing:
        raise ValueError(f"필수 쿠키가 빠져 있습니다: {', '.join(missing)}. 로그인 후 다시 export해주세요.")
    if not cookies:
        raise ValueError("카카오 쿠키가 없습니다. page.kakao.com에서 로그인한 뒤 다시 export해주세요.")
    return cookies


def parse_cookie_export(text: str) -> list[dict]:
    if not (text or "").strip():
        raise ValueError("붙여넣은 내용이 비어 있습니다.")
    if len(text.encode('utf-8')) > 512 * 1024:
        raise ValueError("쿠키 JSON이 너무 큽니다.")
    try:
        return validate_cookies(json.loads(text))
    except json.JSONDecodeError:
        raise ValueError("JSON 형식이 아닙니다. Cookie-Editor의 Export → JSON을 붙여넣어 주세요.") from None


def load_state() -> tuple[list[dict] | None, int, int]:
    with db.read_lock() as conn:
        raw = _get(conn, SETTING_KEY)
        generation = int(_get(conn, _VERSION_KEY) or 0)
        revision = int(_get(conn, _REVISION_KEY) or 0)
    if not raw:
        return None, generation, revision
    try:
        return validate_cookies(json.loads(crypto.decrypt(raw)), require_all=False), generation, revision
    except (ValueError, TypeError, KeyError):
        log.warning("카카오페이지 인증 저장소를 읽지 못했습니다(값 비노출).")
        return None, generation, revision


def load_snapshot() -> tuple[list[dict] | None, int]:
    cookies, generation, _ = load_state()
    return cookies, generation


def load_cookies() -> list[dict] | None:
    return load_snapshot()[0]


def save_cookies(cookies: list[dict], *, expected_version: int | None = None, verified=False) -> int | None:
    cookies = validate_cookies(cookies)
    encrypted = crypto.encrypt(json.dumps(cookies))
    with _credential_transaction() as conn:
        revision = int(_get(conn, _VERSION_KEY) or 0)
        if expected_version is not None and revision != expected_version:
            return None
        revision += 1
        _put(conn, SETTING_KEY, encrypted)
        _put(conn, _VERSION_KEY, str(revision))
        _put(conn, _REVISION_KEY, str(int(_get(conn, _REVISION_KEY) or 0) + 1))
        now = datetime.now(timezone.utc).isoformat()
        _put(conn, _HEALTH_KEY, json.dumps({"state": "healthy" if verified else "unchecked", "credential_version": revision,
             "checked_at": now if verified else None, "last_success_at": now if verified else None, "reason": None}))
        _put(conn, _ALERT_EXPIRED_KEY, None)
        _put(conn, _ALERT_SOON_KEY, None)
        _put(conn, _REFRESH_KEY, None)
        if verified:
            repository.resume_kakao_auth_queue(connection=conn)
    return revision


def delete_cookies() -> None:
    with _credential_transaction() as conn:
        _put(conn, SETTING_KEY, None)
        _put(conn, _VERSION_KEY, str(int(_get(conn, _VERSION_KEY) or 0) + 1))
        _put(conn, _HEALTH_KEY, None)
        _put(conn, _REVISION_KEY, str(int(_get(conn, _REVISION_KEY) or 0) + 1))
        _put(conn, _REFRESH_KEY, None)


def _identity(cookie):
    return cookie['name'], cookie['domain'].lower().lstrip('.'), cookie.get('path', '/')


def _auth_values(cookies):
    return {_identity(c): c['value'] for c in cookies or [] if c['name'] in (*REQUIRED_COOKIES, '_karmt', '_kpawbat_e_v2')}


def _auth_snapshot(cookies):
    return {_identity(c): (c['value'], c.get('expirationDate'), bool(c.get('secure', True)),
                           bool(c.get('hostOnly', False)))
            for c in cookies or [] if c['name'] in (*REQUIRED_COOKIES, '_karmt', '_kpawbat_e_v2')}


def _merge_cookies(current, base, incoming):
    old = {_identity(c): c for c in base}
    updated = {_identity(c): c for c in incoming}
    merged = {_identity(c): c for c in current}
    for key in old.keys() | updated.keys():
        before, after = old.get(key), updated.get(key)
        if before == after:
            continue
        actual = merged.get(key)
        if actual != before:
            continue
        if after is None:
            merged.pop(key, None)
        else:
            merged[key] = after
    return list(merged.values())


def update_cookies(cookies: list[dict], expected_version: int, *, base_cookies: list[dict], strict_auth=False):
    cookies = validate_cookies(cookies, require_all=False)
    with _credential_transaction() as conn:
        raw = _get(conn, SETTING_KEY)
        if int(_get(conn, _VERSION_KEY) or 0) != expected_version or not raw:
            return None
        current = validate_cookies(json.loads(crypto.decrypt(raw)), require_all=False)
        auth_snapshot = _auth_snapshot if strict_auth else _auth_values
        if auth_snapshot(current) != auth_snapshot(base_cookies):
            return None
        result = _merge_cookies(current, base_cookies, cookies)
        revision = int(_get(conn, _REVISION_KEY) or 0)
        if result != current:
            revision += 1
            _put(conn, SETTING_KEY, crypto.encrypt(json.dumps(result)))
            _put(conn, _REVISION_KEY, str(revision))
    return result, revision


def _current_refresh_owner(conn, expected_version, expected_cookies):
    raw = _get(conn, SETTING_KEY)
    if not raw or int(_get(conn, _VERSION_KEY) or 0) != expected_version:
        return None
    current = validate_cookies(json.loads(crypto.decrypt(raw)), require_all=False)
    return current if _auth_snapshot(current) == _auth_snapshot(expected_cookies) else None


def _refresh_fingerprint(cookies):
    values = sorted((list(key), value) for key, value in _auth_values(cookies).items())
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _refresh_metadata(raw):
    try:
        state = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return {}
    if not isinstance(state, dict):
        return {}
    for key in ('attempted_ts', 'next_attempt_at'):
        value = state.get(key, 0)
        if type(value) not in (int, float) or not 0 <= value <= 253402300799:
            return {}
    return state


def refresh_status() -> dict:
    cookies, generation, _ = load_state()
    state = _refresh_metadata(repository.get_setting(_REFRESH_KEY))
    if state.get('credential_version') != generation or state.get('fingerprint') != _refresh_fingerprint(cookies):
        return {'state': 'idle', 'interval_seconds': REFRESH_INTERVAL_SECONDS}
    if state.get('state') == 'running' and state.get('next_attempt_at', 0) <= time.time():
        state.update(state='unknown', reason='refresh_interrupted')
    public_keys = ('state', 'reason', 'credential_version', 'last_attempt_at', 'attempted_ts', 'next_attempt_at', 'last_success_at')
    return {k: state[k] for k in public_keys if k in state} | {'interval_seconds': REFRESH_INTERVAL_SECONDS}


def begin_refresh(expected_version, expected_cookies, *, force=False) -> str | None:
    with _credential_transaction() as conn:
        current = _current_refresh_owner(conn, expected_version, expected_cookies)
        if current is None:
            return None
        previous = _refresh_metadata(_get(conn, _REFRESH_KEY))
        fingerprint = _refresh_fingerprint(current)
        if previous.get('credential_version') != expected_version or previous.get('fingerprint') != fingerprint:
            previous = {}
        now = time.time()
        if now < previous.get('next_attempt_at', 0):
            if not force or previous.get('state') != 'healthy' or now-previous.get('attempted_ts', 0) < 15:
                return None
        attempt_id = uuid4().hex
        _put(conn, _REFRESH_KEY, json.dumps({**previous, 'state': 'running', 'reason': None,
             'credential_version': expected_version, 'fingerprint': fingerprint,
             'last_attempt_at': datetime.now(timezone.utc).isoformat(), 'attempted_ts': now,
             'next_attempt_at': now+60, 'attempt_id': attempt_id}))
    return attempt_id


def finish_refresh(expected_version, expected_cookies, *, attempt_id, state, reason) -> bool:
    assert state in ('reauth_required', 'unknown')
    with _credential_transaction() as conn:
        previous = _refresh_metadata(_get(conn, _REFRESH_KEY))
        current = _current_refresh_owner(conn, expected_version, expected_cookies)
        if current is None or previous.get('attempt_id') != attempt_id:
            return False
        previous.pop('attempt_id')
        _put(conn, _REFRESH_KEY, json.dumps({**previous, 'state': state, 'reason': reason,
             'credential_version': expected_version, 'fingerprint': _refresh_fingerprint(current),
             'next_attempt_at': time.time()+(300 if state == 'unknown' else REFRESH_INTERVAL_SECONDS)}))
        health = json.loads(_get(conn, _HEALTH_KEY) or '{}')
        if state == 'reauth_required' or health.get('state') not in ('healthy', 'reauth_required'):
            _put(conn, _HEALTH_KEY, json.dumps({**health, 'state': state, 'reason': reason,
                 'credential_version': expected_version, 'checked_at': datetime.now(timezone.utc).isoformat()}))
    return True


def promote_refresh(cookies, expected_version, expected_cookies, *, attempt_id, reason=None):
    cookies = validate_cookies(cookies)
    with _credential_transaction() as conn:
        previous = _refresh_metadata(_get(conn, _REFRESH_KEY))
        current = _current_refresh_owner(conn, expected_version, expected_cookies)
        if current is None or previous.get('attempt_id') != attempt_id:
            return None
        merged = _merge_cookies(current, expected_cookies, cookies)
        revision = int(_get(conn, _REVISION_KEY) or 0)
        if merged != current:
            revision += 1
            _put(conn, SETTING_KEY, crypto.encrypt(json.dumps(merged)))
            _put(conn, _REVISION_KEY, str(revision))
        now = datetime.now(timezone.utc).isoformat()
        _put(conn, _HEALTH_KEY, json.dumps({'state': 'healthy', 'reason': None, 'credential_version': expected_version,
             'checked_at': now, 'last_success_at': now}))
        previous.pop('attempt_id')
        if reason is None:
            previous['last_success_at'] = now
        _put(conn, _REFRESH_KEY, json.dumps({**previous, 'state': 'healthy', 'reason': reason,
             'credential_version': expected_version, 'fingerprint': _refresh_fingerprint(merged),
             'next_attempt_at': time.time()+REFRESH_INTERVAL_SECONDS}))
        _put(conn, _ALERT_EXPIRED_KEY, None)
        repository.resume_kakao_auth_queue(connection=conn)
    return merged, revision


def resume_verified_queue(expected_version=None, *, expected_cookies=None) -> bool:
    with _credential_transaction() as conn:
        generation = int(_get(conn, _VERSION_KEY) or 0)
        health = json.loads(_get(conn, _HEALTH_KEY) or '{}')
        raw = _get(conn, SETTING_KEY)
        if (expected_version is not None and generation != expected_version) or not raw:
            return False
        if health.get('state') != 'healthy' or health.get('credential_version') != generation:
            return False
        if expected_cookies is not None and _auth_snapshot(json.loads(crypto.decrypt(raw))) != _auth_snapshot(expected_cookies):
            return False
        repository.resume_kakao_auth_queue(connection=conn)
    return True


def defer_download(item_id: str, failure: KakaoAuthPending) -> tuple[str, str]:
    with _credential_transaction() as conn:
        generation = int(_get(conn, _VERSION_KEY) or 0)
        health = json.loads(_get(conn, _HEALTH_KEY) or '{}')
        replaced = failure.credential_version is not None and failure.credential_version != generation
        recovered = (failure.state in ('reauth_required', 'not_configured')
                     and health.get('state') == 'healthy' and health.get('credential_version') == generation)
        retry = failure.state in ('unknown', 'credentials_changed') or replaced or recovered
        delay = 1 if replaced or recovered or failure.state == 'credentials_changed' else 300
        state = 'retry_wait' if retry else 'blocked_auth'
        message = MESSAGES['credentials_changed'] if replaced or recovered else str(failure)
        repository.update_download_queue(item_id, state=state, reason=message,
            retry_at=time.time()+delay if retry else None, connection=conn)
    return state, message


def record_health(logged_in: bool | None, revision: int | None, reason=None, *, expected_cookies=None) -> bool:
    if revision is None:
        return False
    with _credential_transaction() as conn:
        if int(_get(conn, _VERSION_KEY) or 0) != revision:
            return False
        if expected_cookies is not None:
            raw = _get(conn, SETTING_KEY)
            if not raw or _auth_snapshot(json.loads(crypto.decrypt(raw))) != _auth_snapshot(expected_cookies):
                return False
        previous = json.loads(_get(conn, _HEALTH_KEY) or '{}')
        now = datetime.now(timezone.utc).isoformat()
        state = "healthy" if logged_in is True else "reauth_required" if logged_in is False else "unknown"
        # An inconclusive metadata request must not erase a confirmed rejection.
        if logged_in is None and previous.get('state') == 'reauth_required':
            state = 'reauth_required'
        _put(conn, _HEALTH_KEY, json.dumps({"state": state, "reason": reason, "credential_version": revision,
             "checked_at": now, "last_success_at": now if logged_in is True else previous.get('last_success_at')}))
    return True


def cookie_map(cookies: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in cookies}


def cookie_status(cookies: list[dict] | None, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    names = {c['name'] for c in cookies or []}
    expiries = [c['expirationDate'] for c in cookies or [] if c['name'] in REQUIRED_COOKIES
                and isinstance(c.get('expirationDate'), (int, float)) and not isinstance(c.get('expirationDate'), bool)
                and math.isfinite(c['expirationDate']) and 0 <= c['expirationDate'] <= 253402300799]
    earliest = min(expiries) if expiries else None
    return {"saved": bool(cookies), "missing": [name for name in REQUIRED_COOKIES if name not in names],
            "optional_present": [name for name in OPTIONAL_COOKIES if name in names],
            "expires_at": datetime.fromtimestamp(earliest, timezone.utc).isoformat() if earliest is not None else None,
            "days_left": math.floor((earliest-now)/86400) if earliest is not None else None}


def status() -> dict:
    cookies, revision = load_snapshot()
    raw_health = repository.get_setting(_HEALTH_KEY)
    try:
        health = json.loads(raw_health or '{}')
    except ValueError:
        health = {}
    if health.get('credential_version') != revision:
        health = {}
    state = health.get('state', 'unchecked' if cookies else 'not_configured')
    if cookies is None and repository.get_setting(SETTING_KEY):
        state = 'storage_error'
    if not cookies and state not in ('storage_error', 'reauth_required'):
        state = 'not_configured'
    return {**cookie_status(cookies), **health, 'enabled': repository.get_setting('kakao_webtoons_enabled') == '1', 'state': state, 'message': MESSAGES.get(state, MESSAGES['unknown']), 'auto_refresh': refresh_status()}


def health_alert_message(logged_in: bool | None, days_left: int | None) -> str | None:
    if logged_in is False:
        return "🍪 **카카오페이지 쿠키 만료** — 로그인이 풀렸습니다. 설정에서 새 쿠키를 다시 등록해주세요. 다운로드 대기열은 보존됩니다."
    if logged_in is True and days_left is not None and days_left <= _EXPIRY_WARNING_DAYS:
        return f"🍪 **카카오페이지 로그인 쿠키가 곧 만료됩니다** (약 {max(days_left,0)}일 남음). 설정에서 새 쿠키를 다시 등록해주세요."
    return None


def _recently_sent(key: str) -> bool:
    try:
        return time.time() - float(repository.get_setting(key) or 0) < _ALERT_INTERVAL_SECONDS
    except ValueError:
        return False


async def notify_if_needed(session, logged_in: bool | None, days_left: int | None) -> bool:
    if logged_in is True:
        repository.set_setting(_ALERT_EXPIRED_KEY, None)
    message = health_alert_message(logged_in, days_left)
    if message is None:
        return False
    key = _ALERT_EXPIRED_KEY if logged_in is False else _ALERT_SOON_KEY
    if _recently_sent(key) or not discord_config.get_webhook_url():
        return False
    if not await discord_notify.send_webhook_notification(session, message):
        return False
    repository.set_setting(key, str(time.time()))
    log.warning("카카오페이지 쿠키 알림 전송: %s", "로그인 풀림" if logged_in is False else "곧 만료")
    return True
