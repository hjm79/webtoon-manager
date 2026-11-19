"""Refresh saved KakaoPage sessions without exposing or replacing unverified cookies."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from app import kakao_page_auth as auth, kakao_page_download as kp, repository

log = logging.getLogger(__name__)

REFRESH_URL = "https://bff-page.kakao.com/api/refresh_token"
_refresh_lock = asyncio.Lock()
_completion_sequence = 0
_last_completion = None


def _result(state, reason, *, refreshed=False):
    return {"state": state, "reason": reason, "refreshed": refreshed}


def _promote(candidate, cookies, generation, attempt_id, *, reason, refreshed, start_worker):
    if auth.promote_refresh(candidate, generation, cookies, attempt_id=attempt_id,
                            reason=None if refreshed else reason) is None:
        return _result("credentials_changed", "credential_version_changed")
    if start_worker:
        from app.scheduler import download_job

        download_job.start_queued_downloads()
    return _result("healthy", reason, refreshed=refreshed)


async def _attempt(session, cookies, generation, attempt_id, timeout_seconds, start_worker):
    def finish(state, reason):
        if not auth.finish_refresh(generation, cookies, attempt_id=attempt_id, state=state, reason=reason):
            return _result("credentials_changed", "refresh_superseded")
        return _result(state, reason)

    candidate = kp.KakaoPageClient(session, auth.cookie_map(cookies), timeout_seconds,
                                   cookie_entries=cookies, credential_version=None)
    headers = candidate._headers(url=REFRESH_URL)
    headers["Accept"] = "application/json"
    candidate._last_url = REFRESH_URL
    try:
        async with session.post(REFRESH_URL, headers=headers, allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=5)) as response:
            status = response.status
            if status == 200:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, TypeError):
                    return finish("unknown", "refresh_invalid_response")
                if not isinstance(payload, dict):
                    return finish("unknown", "refresh_invalid_response")
                code = str(payload.get("result_code"))
                rejected = code in ("401", "-401", "UNAUTHORIZED", "LOGIN_REQUIRED") or (
                    code == "-100" and payload.get("message_key") == "api_user_authorize_not_found")
                if not rejected:
                    token = payload.get("access_token")
                    if code != "0" or not isinstance(token, str) or not token.strip():
                        return finish("unknown", "refresh_invalid_response")
                    candidate._absorb_cookies(response)
            else:
                rejected = status in (401, 403)
    except asyncio.CancelledError:
        finish("unknown", "refresh_cancelled")
        raise
    except Exception as exc:
        log.warning("카카오페이지 세션 갱신 요청 실패: %s", type(exc).__name__)
        return finish("unknown", "refresh_network_error")

    if rejected:
        # A refresh cookie may expire before the access session. Probe only the
        # original jar; cookies from a rejected refresh must never be promoted.
        try:
            logged_in = await candidate.check_login()
        except asyncio.CancelledError:
            finish("unknown", "refresh_cancelled")
            raise
        except Exception as exc:
            log.warning("카카오페이지 기존 세션 확인 실패: %s", type(exc).__name__)
            logged_in = None
        if logged_in is False:
            return finish("reauth_required", "refresh_rejected")
        if logged_in is None:
            return finish("unknown", "refresh_profile_unknown")
    elif status != 200:
        if status == 429:
            reason = "refresh_rate_limited"
        elif 300 <= status < 400:
            reason = "refresh_redirect"
        else:
            reason = "refresh_server_error" if status >= 500 else "refresh_invalid_response"
        return finish("unknown", reason)
    else:
        try:
            logged_in = await candidate.check_login()
        except asyncio.CancelledError:
            finish("unknown", "refresh_cancelled")
            raise
        except Exception as exc:
            log.warning("카카오페이지 갱신 세션 확인 실패: %s", type(exc).__name__)
            return finish("unknown", "refresh_profile_error")
        if logged_in is not True:
            reason = "refreshed_session_unverified" if logged_in is False else "refresh_profile_unknown"
            return finish("unknown", reason)

    try:
        validated = auth.validate_cookies(candidate.cookie_entries)
    except ValueError:
        return finish("unknown", "refresh_incomplete_cookies")
    reason = "refresh_rejected_session_valid" if rejected else "session_refreshed"
    return _promote(validated, cookies, generation, attempt_id, reason=reason, refreshed=not rejected,
                    start_worker=start_worker)


async def refresh_saved_session(*, session=None, timeout_seconds=10, force=False, start_worker=True):
    """Single-flight refresh, candidate profile verification, then atomic promotion."""
    global _completion_sequence, _last_completion

    initial = auth.load_state()
    initial_key = initial[1:]
    observed_sequence = _completion_sequence
    if not initial[0]:
        if auth.status()["state"] == "storage_error":
            return _result("unknown", "credential_storage_error")
        return _result("not_configured", "cookies_missing")
    async with _refresh_lock:
        current = auth.load_state()
        if observed_sequence != _completion_sequence and _last_completion is not None:
            previous_input, previous_output, result = _last_completion
            if initial_key == previous_input and current[1:] == previous_output:
                if start_worker and result['state'] == 'healthy':
                    from app.scheduler import download_job

                    download_job.start_queued_downloads()
                return dict(result)
        if current[1] != initial[1] or not current[0]:
            return _result("credentials_changed", "credential_version_changed")
        cookies, generation, _ = current
        attempt_id = auth.begin_refresh(generation, cookies, force=force)
        if not attempt_id:
            status = auth.refresh_status()
            if auth.load_state()[1:] != current[1:]:
                return _result("credentials_changed", "credential_version_changed")
            if status.get("state") == "reauth_required":
                return _result("reauth_required", status.get("reason") or "refresh_rejected")
            return _result("deferred", status.get("reason") or "refresh_backoff")
        if session is None:
            async with kp.new_session() as owned_session:
                result = await _attempt(owned_session, cookies, generation, attempt_id, timeout_seconds, start_worker)
        else:
            result = await _attempt(session, cookies, generation, attempt_id, timeout_seconds, start_worker)
        _completion_sequence += 1
        _last_completion = (current[1:], auth.load_state()[1:], dict(result))
        return result


async def run_auto_refresh_job():
    """Scheduled maintenance performs no network request for an unused integration."""
    if repository.get_setting("kakao_webtoons_enabled") != "1":
        return _result("not_configured", "integration_disabled")
    if not auth.load_state()[0]:
        return _result("not_configured", "cookies_missing")
    return await refresh_saved_session()
