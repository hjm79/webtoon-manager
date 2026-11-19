"""Official refresh contract, isolated candidates and single-flight credential fencing."""

import asyncio
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from app import db, kakao_page_auth as auth, kakao_session_refresh as refresh, repository, subscription_scope
from app.kakao_page_download.models import PROFILE_URL
from app.kakao_page_download.runner import download_episode
from app.scheduler import download_job

db.get_connection()

GOOD = {"result_code": 0, "profile": {"uid": 1}}
REJECT = {"result_code": -100, "message_key": "api_user_authorize_not_found"}
REFRESHED = {"result_code": 0, "access_token": "RESPONSE_TOKEN_NOT_FOR_STORAGE"}


def exported(prefix="ORIGINAL"):
    return [{"name": name, "value": f"{prefix}_{index}", "domain": ".kakao.com", "path": "/",
             "secure": True, "expirationDate": time.time() + 20 * 86400}
            for index, name in enumerate(auth.REQUIRED_COOKIES)]


class Response:
    def __init__(self, data=None, status=200, cookies=(), gate=None):
        self.data, self.status, self.gate = data, status, gate
        self.cookies = SimpleCookie()
        for cookie in cookies:
            self.cookies.load(cookie)
        self.url = refresh.REFRESH_URL

    async def json(self, **kwargs):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    async def __aenter__(self):
        if self.gate:
            entered, release = self.gate
            entered.set()
            await release.wait()
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, response, profile=GOOD):
        self.response, self.profile = response, profile
        self.requests = []
        self.before_profile = None

    def post(self, url, **kwargs):
        self.requests.append((str(url), kwargs))
        if str(url) == refresh.REFRESH_URL:
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        assert str(url) == PROFILE_URL, "Refresh replayed an unrelated request"
        if self.before_profile:
            self.before_profile(kwargs)
        return self.profile if isinstance(self.profile, Response) else Response(self.profile)


def stored():
    return repository.get_setting(auth.SETTING_KEY), auth.load_state()[1:]


def fresh(prefix="ORIGINAL"):
    auth.save_cookies(exported(prefix), verified=True)
    return stored()


async def verified_success():
    before = fresh()
    before_health = auth.status()
    session = Session(Response(REFRESHED, cookies=(
        "_kpwtkn=ROTATED_ACCESS; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure",
        "_kpawbat_e_v2=ROTATED_REFRESH; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure")))

    def inspect_profile(kwargs):
        assert "_kpwtkn=ROTATED_ACCESS" in kwargs["headers"]["Cookie"]
        assert stored() == before, "Unverified candidate changed the credential store"
        assert auth.status()["state"] == before_health["state"]

    session.before_profile = inspect_profile
    with patch.object(download_job, "start_queued_downloads") as start:
        result = await refresh.refresh_saved_session(session=session)
        assert start.call_count == 1
    assert result == {"state": "healthy", "reason": "session_refreshed", "refreshed": True}
    saved, generation, revision = auth.load_state()
    assert generation == before[1][0] and revision == before[1][1] + 1
    assert auth.cookie_map(saved)["_kpwtkn"] == "ROTATED_ACCESS"
    assert "RESPONSE_TOKEN_NOT_FOR_STORAGE" not in str(saved)
    url, request = session.requests[0]
    assert url == refresh.REFRESH_URL and "data" not in request and "json" not in request
    assert request["allow_redirects"] is False and request["timeout"].total == 5
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["Origin"] == "https://page.kakao.com"
    assert request["headers"]["Referer"] == "https://page.kakao.com/"
    assert not any(c["name"] == "_karmt" for c in saved)
    print("Verified refresh uses the official bodyless POST and only promotes after profile success")


async def rejected_and_temporary_responses():
    cases = [
        (Response(status=401), REJECT, "reauth_required"),
        (Response(status=403), REJECT, "reauth_required"),
        (Response(status=401), GOOD, "healthy"),
        (Response(status=401), {"result_code": 500}, "unknown"),
        (Response(status=429), GOOD, "unknown"),
        (Response(status=503), GOOD, "unknown"),
        (Response(status=302), GOOD, "unknown"),
        (Response(ValueError("Malformed JSON")), GOOD, "unknown"),
        (Response([]), GOOD, "unknown"),
        (Response({"result_code": 0}), GOOD, "unknown"),
        (Response({"result_code": 1, "access_token": "BAD"}), GOOD, "unknown"),
        (aiohttp.ClientConnectionError(), GOOD, "unknown"),
        (Response(REFRESHED, cookies=("_kpwtkn=UNVERIFIED; Domain=.kakao.com; Path=/",)), REJECT, "unknown"),
        (Response(REFRESHED), {"result_code": 500}, "unknown"),
        (Response(REFRESHED, cookies=("_kpwtkn=; Domain=.kakao.com; Path=/; Max-Age=0",)), GOOD, "unknown"),
    ]
    for index, (response, profile, state) in enumerate(cases):
        before = fresh(f"CASE_{index}")
        session = Session(response, profile)
        with patch.object(download_job, "start_queued_downloads") as start:
            result = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
        assert result["state"] == state and result["refreshed"] is False, (index, result)
        assert auth.status()["state"] == ("reauth_required" if state == "reauth_required" else "healthy")
        assert auth.refresh_status()["state"] == state
        assert stored() == before, "Failed or rejected refresh changed saved cookies"
        assert not start.called
        if state == "healthy":
            assert result["reason"] == "refresh_rejected_session_valid"
        if getattr(response, "status", None) in (302, 429, 503):
            assert len(session.requests) == 1
    print("Rejected, throttled, redirected, malformed and unverified candidates preserve originals")


async def concurrent_refresh_and_manual_replacement():
    fresh("CONCURRENT")
    entered, release = asyncio.Event(), asyncio.Event()
    session = Session(Response(REFRESHED, gate=(entered, release)))
    first = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
    await entered.wait()
    second = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert all(r["refreshed"] for r in results)
    assert sum(url == refresh.REFRESH_URL for url, _ in session.requests) == 1

    for profile in (GOOD, REJECT):
        fresh("STALE")
        entered, release = asyncio.Event(), asyncio.Event()
        session = Session(Response(REFRESHED, gate=(entered, release)), profile)
        task = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
        await entered.wait()
        replacement = fresh("MANUALLY_REPLACED")
        release.set()
        result = await task
        assert result["state"] == "credentials_changed" and not result["refreshed"]
        assert stored() == replacement and auth.status()["state"] == "healthy"
    print("Concurrent callers share one refresh and stale results cannot overwrite a manual replacement")


async def timer_and_backoff():
    fresh("BACKOFF")
    session = Session(Response(status=503))
    await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
    count = len(session.requests)
    result = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
    assert result["state"] == "deferred" and len(session.requests) == count

    fresh("REJECTED")
    session = Session(Response(status=401), REJECT)
    first = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
    assert first['state'] == 'reauth_required'
    second = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
    assert second == first and len(session.requests) == 2

    repository.set_setting("kakao_webtoons_enabled", "0")
    with patch.object(refresh, "refresh_saved_session", new_callable=AsyncMock) as operation:
        result = await refresh.run_auto_refresh_job()
        assert result["state"] == "not_configured" and not operation.called
        repository.set_setting("kakao_webtoons_enabled", "1")
        with patch.object(auth, "load_state", return_value=(None, 1, 1)):
            result = await refresh.run_auto_refresh_job()
            assert result["state"] == "not_configured" and not operation.called
        await refresh.run_auto_refresh_job()
        assert operation.await_count == 1
    print("Persistent backoff also constrains forced calls; timer skips disabled and unconfigured integrations")


async def fallback_cooldown_and_alert_suppression():
    fresh("FALLBACK")
    stamp = str(time.time())
    repository.set_setting(auth._ALERT_SOON_KEY, stamp)
    session = Session(Response(status=401), GOOD)
    fallback = await refresh.refresh_saved_session(session=session, start_worker=False)
    assert fallback['state'] == 'healthy' and not fallback['refreshed']
    assert not auth.refresh_status().get('last_success_at')
    assert repository.get_setting(auth._ALERT_SOON_KEY) == stamp
    assert (await refresh.refresh_saved_session(session=session, force=True, start_worker=False))['state'] == 'deferred'
    cookies, generation = auth.load_snapshot()
    assert auth.record_health(False, generation, 'session_rejected', expected_cookies=cookies)
    with patch.object(auth.time, 'time', return_value=time.time() + 20):
        session = Session(Response(REFRESHED), GOOD)
        result = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
    assert result['refreshed'] and auth.refresh_status()['last_success_at']
    assert repository.get_setting(auth._ALERT_SOON_KEY) == stamp


def expired_attempt_cannot_overwrite_replacement():
    fresh("ATTEMPT")
    cookies, generation, _ = auth.load_state()
    # Settings restore or an interrupted write must not disable future refreshes.
    repository.set_setting(auth._REFRESH_KEY, '[]')
    assert auth.refresh_status()['state'] == 'idle'
    first = auth.begin_refresh(generation, cookies)
    assert first and 'attempt_id' not in auth.refresh_status()
    with patch.object(auth.time, 'time', return_value=time.time() + 61):
        assert auth.refresh_status()['reason'] == 'refresh_interrupted'
        second = auth.begin_refresh(generation, cookies)
    assert second and second != first
    assert not auth.finish_refresh(generation, cookies, attempt_id=first, state='reauth_required', reason='stale')
    assert auth.promote_refresh(cookies, generation, cookies, attempt_id=first) is None
    assert auth.refresh_status()['state'] == 'running' and auth.status()['state'] == 'healthy'
    assert auth.promote_refresh(cookies, generation, cookies, attempt_id=second) is not None


async def credential_change_cancels_other_images():
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class Client:
        async def viewer_image_urls(self, *args):
            return ['first', 'stale', 'in-flight']

        async def download_image(self, url):
            if url == 'first':
                return b'image', 'image/jpeg'
            if url == 'stale':
                await sibling_started.wait()
                raise auth.KakaoAuthPending('credentials_changed')
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                sibling_cancelled.set()

    try:
        await asyncio.wait_for(download_episode(Client(), 1, SimpleNamespace(product_id=1), Path('.')), timeout=2)
    except auth.KakaoAuthPending as exc:
        assert exc.state == 'credentials_changed'
    else:
        raise AssertionError('Credential change did not stop the episode')
    assert sibling_cancelled.is_set(), 'A sibling image request outlived the episode failure'


async def blocked_queue_recovery():
    fresh("QUEUE")
    title_id = "990093"
    rows = [{"episode_no": number, "subtitle": str(number), "available": True, "owned": False}
            for number in range(1, 5)]
    scope = subscription_scope.make_choice("kakao", title_id, "selected", rows, episode_nos=[2, 4])
    repository.activate_scoped_subscription("kakao", title_id, "자동 갱신 검증", "", "", scope, enqueue=True)
    item = repository.list_download_queue()[0]
    repository.update_download_queue(item["id"], state="blocked_auth", reason="Expired")
    scope_before = repository.get_setting(f"subscription_scope:kakao:{title_id}")
    await refresh.refresh_saved_session(session=Session(Response(status=401), REJECT), start_worker=False)
    assert repository.list_download_queue()[0]["state"] == "blocked_auth"
    assert auth.status()["state"] == "reauth_required"
    # Simulate the next scheduled opportunity without waiting for real time.
    with patch.object(auth.time, "time", return_value=time.time() + 3600):
        def assert_committed():
            assert auth.status()["state"] == "healthy"
            assert repository.list_download_queue()[0]["state"] == "queued"

        with patch.object(download_job, "start_queued_downloads", side_effect=assert_committed) as start:
            profile = Response(GOOD, cookies=("_kpwtkn=PROFILE_ROTATED; Domain=.kakao.com; Path=/; Secure",))
            result = await refresh.refresh_saved_session(session=Session(Response(status=401), profile))
        assert result["state"] == "healthy" and not result["refreshed"] and start.call_count == 1
        assert result["reason"] == "refresh_rejected_session_valid"
        assert auth.cookie_map(auth.load_state()[0])["_kpwtkn"] == "PROFILE_ROTATED"
    assert repository.get_setting(f"subscription_scope:kakao:{title_id}") == scope_before
    assert subscription_scope.allowed_numbers("kakao", title_id, [1, 2, 3, 4]) == {2, 4}
    repository.update_download_queue(item["id"])
    print("Confirmed refresh resumes a blocked queue after commit while preserving its selected episode scope")


async def mixed_worker_singleflight():
    before = fresh("MIXED_WORKERS")
    entered, release = asyncio.Event(), asyncio.Event()
    session = Session(Response(REFRESHED, cookies=(
        "_kpwtkn=MIXED_ROTATED; Domain=.kakao.com; Path=/; Secure",), gate=(entered, release)))

    def assert_committed():
        assert auth.cookie_map(auth.load_state()[0])["_kpwtkn"] == "MIXED_ROTATED"
        assert auth.load_state()[2] == before[1][1] + 1
        assert auth.status()["state"] == "healthy"

    with patch.object(download_job, "start_queued_downloads", side_effect=assert_committed) as start:
        first = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
        await entered.wait()
        second = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=True))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
    assert all(result["refreshed"] for result in results)
    assert sum(url == refresh.REFRESH_URL for url, _ in session.requests) == 1
    assert start.call_count == 1
    print("A concurrent worker-enabled caller starts the queue once after shared refresh commit")


async def cancellation_and_deletion():
    for phase in ("refresh", "profile"):
        before = fresh(f"CANCEL_{phase}")
        entered, release = asyncio.Event(), asyncio.Event()
        gate = (entered, release)
        response = Response(REFRESHED, gate=gate if phase == "refresh" else None, cookies=(
            "_kpwtkn=CANCELLED_CANDIDATE; Domain=.kakao.com; Path=/; Secure",))
        profile = Response(GOOD, gate=gate if phase == "profile" else None)
        session = Session(response, profile)
        task = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Refresh cancellation was swallowed")
        assert stored() == before
        assert auth.refresh_status()["state"] == "unknown"
        assert auth.refresh_status()["reason"] == "refresh_cancelled"
        count = len(session.requests)
        result = await refresh.refresh_saved_session(session=session, force=True, start_worker=False)
        assert result["state"] == "deferred" and len(session.requests) == count

    for profile in (GOOD, REJECT):
        fresh("DELETE_PENDING")
        entered, release = asyncio.Event(), asyncio.Event()
        session = Session(Response(REFRESHED, gate=(entered, release)), profile)
        task = asyncio.create_task(refresh.refresh_saved_session(session=session, force=True, start_worker=False))
        await entered.wait()
        auth.delete_cookies()
        deleted = stored()
        release.set()
        result = await task
        assert result["state"] == "credentials_changed" and not result["refreshed"]
        assert stored() == deleted and auth.load_state()[0] is None
        assert auth.status()["state"] == "not_configured"
    print("Cancellation preserves credentials and backoff; deletion cannot be undone by pending refresh results")


def promotion_rollback_and_restart_reconciliation():
    fresh("ROLLBACK")
    title_id = "990094"
    rows = [{"episode_no": number, "subtitle": str(number), "available": True, "owned": False}
            for number in range(1, 4)]
    choice = subscription_scope.make_choice("kakao", title_id, "selected", rows, episode_nos=[1, 3])
    repository.activate_scoped_subscription("kakao", title_id, "승격 롤백 검증", "", "", choice, enqueue=True)
    item = repository.list_download_queue()[0]
    repository.update_download_queue(item["id"], state="blocked_auth", reason="Preserved pending restart")
    cookies, generation, _ = auth.load_state()
    candidate = [dict(cookie) for cookie in cookies]
    next(cookie for cookie in candidate if cookie["name"] == "_kpwtkn")["value"] = "ROLLBACK_CANDIDATE"
    attempt_id = auth.begin_refresh(generation, cookies)
    before_store = stored()
    before_health = auth.status()
    before_queue = repository.list_download_queue()
    before_refresh = auth.refresh_status()
    scope_before = repository.get_setting(f"subscription_scope:kakao:{title_id}")
    with patch.object(repository, "resume_kakao_auth_queue", side_effect=RuntimeError("Injected transaction failure")):
        try:
            auth.promote_refresh(candidate, generation, cookies, attempt_id=attempt_id)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Injected transaction failure did not propagate")
    assert stored() == before_store and auth.status() == before_health
    assert repository.list_download_queue() == before_queue and auth.refresh_status() == before_refresh
    assert auth.cookie_map(auth.load_state()[0])["_kpwtkn"] != "ROLLBACK_CANDIDATE"

    # A fresh Python process must reconcile persisted healthy credentials and a
    # blocked queue without depending on this process's refresh result cache.
    code = (
        "from app import db, kakao_page_auth as auth; "
        "db.get_connection(); "
        "assert auth.status()['state'] == 'healthy'; "
        "assert auth.resume_verified_queue()"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    queue = repository.list_download_queue()
    assert len(queue) == 1 and queue[0]["state"] == "queued"
    assert repository.get_setting(f"subscription_scope:kakao:{title_id}") == scope_before
    assert subscription_scope.allowed_numbers("kakao", title_id, [1, 2, 3]) == {1, 3}
    assert stored() == before_store
    repository.update_download_queue(item["id"])
    print("Failed promotion rolls back credentials and queue; a new process reconciles a healthy blocked queue")


async def main():
    await verified_success()
    await rejected_and_temporary_responses()
    await concurrent_refresh_and_manual_replacement()
    await timer_and_backoff()
    await fallback_cooldown_and_alert_suppression()
    expired_attempt_cannot_overwrite_replacement()
    await credential_change_cancels_other_images()
    await blocked_queue_recovery()
    await mixed_worker_singleflight()
    await cancellation_and_deletion()
    promotion_rollback_and_restart_reconciliation()


asyncio.run(main())
print("All Kakao automatic session refresh checks passed")
