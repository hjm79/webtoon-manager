"""Owned clients recover through the real refresh service without replaying mutations."""

import asyncio
import time
from http.cookies import SimpleCookie
from unittest.mock import patch

from app import db, kakao_page_auth as auth, kakao_page_download as kp, repository, schedule_config
from app import kakao_session_refresh as refresh
from app.kakao_page_download.models import PRODUCT_LIST_URL, PROFILE_URL, TICKET_USE_URL
from app.scheduler import download_job, schedule

db.get_connection()

GOOD = {"result_code": 0, "profile": {"uid": 19}}
REJECT = {"result_code": -100, "message_key": "api_user_authorize_not_found"}
REFRESHED = {"result_code": 0, "access_token": "UNSTORED_RESPONSE_TOKEN"}
ROTATED_COOKIE = "_kpwtkn=ROTATED_ACCESS; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure"
REFRESH_COOKIE = {"name": "_kpawbat_e_v2", "value": "SAVED_REFRESH", "domain": ".kakao.com", "path": "/", "secure": True}


class Response:
    def __init__(self, data=None, *, status=200, cookies=(), error=None):
        self.data, self.status, self.error = data, status, error
        self.cookies = SimpleCookie()
        for cookie in cookies:
            self.cookies.load(cookie)
        self.url = None

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.data


class Session:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def request(self, method, url, kwargs):
        assert self.steps, f"Unexpected extra request: {method} {url}"
        expected_method, expected_url, response = self.steps.pop(0)
        assert (method, str(url)) == (expected_method, expected_url)
        assert kwargs["allow_redirects"] is False
        self.requests.append((method, str(url), kwargs))
        response.url = str(url)
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, kwargs)


def fresh(prefix, *, expired_access=False):
    cookies = [{"name": name, "value": f"{prefix}_{index}", "domain": ".kakao.com", "path": "/",
                "secure": True, "expirationDate": time.time() + 10 * 86400}
               for index, name in enumerate(auth.REQUIRED_COOKIES)]
    if expired_access:
        next(cookie for cookie in cookies if cookie["name"] == "_kpwtkn")["expirationDate"] = time.time() - 60
    cookies.append(REFRESH_COOKIE)
    auth.save_cookies(cookies, verified=True)
    return auth.load_state()


def assert_rotated_client(client, original):
    cookies, generation, revision = auth.load_state()
    assert generation == original[1] == client.credential_version
    assert revision == original[2] + 1 == client.cookie_revision
    assert auth.cookie_map(cookies)["_kpwtkn"] == client.cookies["_kpwtkn"] == "ROTATED_ACCESS"
    assert auth.status()["state"] == "healthy"
    assert client.last_problem is None


async def owned_login_recovers():
    original = fresh("LOGIN")
    session = Session([
        ("POST", PROFILE_URL, Response(status=401, cookies=("_kpawbat_e_v2=; Domain=.kakao.com; Path=/; Max-Age=0",))),
        ("POST", refresh.REFRESH_URL, Response(REFRESHED, cookies=(ROTATED_COOKIE,))),
        ("POST", PROFILE_URL, Response(GOOD)),
    ])
    client = kp.client_from_saved_cookies(session, 2)
    with patch.object(download_job, "start_queued_downloads") as start:
        assert await client.check_login() is True
        assert start.call_count == 1
    assert not session.steps
    assert "_kpawbat_e_v2=SAVED_REFRESH" in session.requests[1][2]["headers"]["Cookie"]
    assert "_kpwtkn=ROTATED_ACCESS" in session.requests[-1][2]["headers"]["Cookie"]
    assert_rotated_client(client, original)
    print("Owned profile rejection refreshes and verifies credentials without changing their generation")


async def stale_response_cannot_undo_expiry_renewal():
    async def run_case(result, response_cookies):
        original = fresh("SAME_VALUE", expired_access=True)
        access_token = auth.cookie_map(original[0])["_kpwtkn"]
        entered, release = asyncio.Event(), asyncio.Event()

        class DelayedProfile(Response):
            async def json(self, **kwargs):
                entered.set()
                await release.wait()
                return self.data

        old_session = Session([("POST", PROFILE_URL, DelayedProfile(result, cookies=response_cookies))])
        old_client = kp.client_from_saved_cookies(old_session, 2)
        old_request = asyncio.create_task(old_client.check_login())
        await entered.wait()
        assert "_kpwtkn=" not in old_session.requests[0][2]["headers"]["Cookie"]
        new_cookie = f"_kpwtkn={access_token}; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure"
        session = Session([
            ("POST", refresh.REFRESH_URL, Response(REFRESHED, cookies=(new_cookie,))),
            ("POST", PROFILE_URL, Response(GOOD)),
        ])
        recovered = await refresh.refresh_saved_session(session=session, start_worker=False)
        assert recovered["state"] == "healthy" and not session.steps
        rotated = auth.load_state()
        assert rotated[1] == original[1] and rotated[2] == original[2] + 1
        release.set()
        try:
            await old_request
        except auth.KakaoAuthPending as exc:
            assert exc.state == "credentials_changed"
        else:
            raise AssertionError("A late response was accepted after access-cookie expiry was renewed")
        assert not old_session.steps
        assert auth.load_state() == rotated and auth.status()["state"] == "healthy"

    await run_case(REJECT, ())
    await run_case(GOOD, ("_kpwtkn=STALE_RESPONSE; Domain=.kakao.com; Path=/; Max-Age=1; Secure",))
    print("A late 401-equivalent or Set-Cookie cannot undo a same-value access-cookie renewal")


async def failed_image_cannot_store_cookies():
    original = fresh("IMAGE")
    url = "https://page-edge.kakao.com/sdownload/resource?signature=TEST_SIGNATURE"
    session = Session([("GET", url, Response(status=302, cookies=(ROTATED_COOKIE,)))])
    with patch("app.kakao_page_download.client._IMAGE_RETRIES", 1):
        assert await kp.client_from_saved_cookies(session, 2).download_image(url) is None
    assert not session.steps
    assert auth.load_state() == original


async def readonly_request_replays_once():
    original = fresh("READ")
    expected = {"result_code": 0, "result": {"list": [], "has_next": False}}
    session = Session([
        ("GET", PRODUCT_LIST_URL, Response(status=401)),
        ("POST", refresh.REFRESH_URL, Response(REFRESHED, cookies=(ROTATED_COOKIE,))),
        ("POST", PROFILE_URL, Response(GOOD)),
        ("GET", PRODUCT_LIST_URL, Response(expected)),
    ])
    client = kp.client_from_saved_cookies(session, 2)
    with patch.object(download_job, "start_queued_downloads"):
        status, data = await client._request_json("GET", PRODUCT_LIST_URL, params={"series_id": 19}, label="read")
    assert status == 200 and data == expected and not session.steps
    gets = [kwargs for method, url, kwargs in session.requests if method == "GET"]
    assert len(gets) == 2 and gets[0]["params"] == gets[1]["params"] == {"series_id": 19}
    assert "_kpwtkn=ROTATED_ACCESS" not in gets[0]["headers"]["Cookie"]
    assert "_kpwtkn=ROTATED_ACCESS" in gets[1]["headers"]["Cookie"]
    assert_rotated_client(client, original)

    fresh("STILL_REJECTED")
    session = Session([
        ("GET", PRODUCT_LIST_URL, Response(status=401)),
        ("POST", refresh.REFRESH_URL, Response(REFRESHED, cookies=(ROTATED_COOKIE,))),
        ("POST", PROFILE_URL, Response(GOOD)),
        ("GET", PRODUCT_LIST_URL, Response(status=401)),
    ])
    with patch.object(download_job, "start_queued_downloads"):
        try:
            await kp.client_from_saved_cookies(session, 2)._request_json("GET", PRODUCT_LIST_URL, label="still rejected")
        except auth.KakaoAuthPending as exc:
            assert exc.state == "unknown" and exc.reason == "request_rejected_after_refresh"
        else:
            raise AssertionError("A repeated read rejection was accepted")
    assert not session.steps and len(session.requests) == 4
    assert auth.status()["state"] == "healthy"
    print("Read-only GET retries once with rotated cookies and stops if the retry is also rejected")


async def mutation_is_not_replayed():
    original = fresh("MUTATION")
    session = Session([
        ("POST", TICKET_USE_URL, Response(REJECT)),
        ("POST", refresh.REFRESH_URL, Response(REFRESHED, cookies=(ROTATED_COOKIE,))),
        ("POST", PROFILE_URL, Response(GOOD)),
    ])
    client = kp.client_from_saved_cookies(session, 2)
    with patch.object(download_job, "start_queued_downloads"):
        try:
            await client._request_json("POST", TICKET_USE_URL, data={"product_id": 19}, label="ticket use")
        except auth.KakaoAuthPending as exc:
            assert exc.state == "credentials_changed" and exc.reason == "session_refreshed"
        else:
            raise AssertionError("A rejected mutation was replayed or silently accepted")
    assert not session.steps
    assert sum(url == TICKET_USE_URL for _, url, _ in session.requests) == 1
    assert_rotated_client(client, original)

    original = fresh("MUTATION_WITH_VALID_PROFILE")
    session = Session([
        ("POST", TICKET_USE_URL, Response(REJECT)),
        ("POST", refresh.REFRESH_URL, Response(status=401)),
        ("POST", PROFILE_URL, Response(GOOD)),
    ])
    with patch.object(download_job, "start_queued_downloads"):
        try:
            await kp.client_from_saved_cookies(session, 2)._request_json("POST", TICKET_USE_URL, label="ticket use")
        except auth.KakaoAuthPending as exc:
            assert exc.state == "unknown" and exc.reason == "request_rejected_after_refresh"
        else:
            raise AssertionError("Rejected mutation with the same valid profile did not back off")
    assert not session.steps and auth.load_state() == original
    assert auth.status()["state"] == "healthy"
    print("A rejected ticket POST refreshes authentication but never repeats the mutation")


async def timeout_preserves_credentials_and_backoff():
    original = fresh("TIMEOUT")
    encrypted = repository.get_setting(auth.SETTING_KEY)
    session = Session([
        ("POST", PROFILE_URL, Response(REJECT)),
        ("POST", refresh.REFRESH_URL, Response(error=asyncio.TimeoutError())),
        ("POST", PROFILE_URL, Response(REJECT)),
    ])
    client = kp.client_from_saved_cookies(session, 2)
    with patch.object(download_job, "start_queued_downloads") as start:
        assert await client.check_login() is None
        state = auth.refresh_status()
        assert state["state"] == "unknown" and state["next_attempt_at"] > time.time()
        assert await client.check_login() is None
        assert not start.called
    assert not session.steps and len(session.requests) == 3
    assert session.requests[1][2]["timeout"].total == 5
    assert repository.get_setting(auth.SETTING_KEY) == encrypted
    assert auth.load_state() == original
    print("Refresh timeout preserves saved credentials and forced client recovery respects persistent backoff")


def internal_job_survives_rescheduling():
    schedule_config.set_download_schedules([
        schedule_config.JobSchedule(mode="interval", interval_minutes=137, target="kakao"),
        schedule_config.JobSchedule(mode="off", target="naver"),
    ])
    schedule_config.set_schedule("discovery_job", schedule_config.JobSchedule(mode="off"))
    keys = ("schedule_download_job", "schedule_discovery_job")
    before = {key: repository.get_setting(key) for key in keys}
    scheduler = schedule.create_scheduler()
    refresh_job = scheduler.get_job("kakao_session_refresh")
    assert refresh_job is not None and refresh_job.func is refresh.run_auto_refresh_job
    assert refresh_job.max_instances == 1 and refresh_job.coalesce is True
    for _ in range(2):
        schedule.reschedule_all(scheduler)
        assert scheduler.get_job("kakao_session_refresh") is refresh_job
        assert sum(job.id == "kakao_session_refresh" for job in scheduler.get_jobs()) == 1
        download = scheduler.get_job("download_job:0")
        assert download.trigger.interval.total_seconds() == 137 * 60
        assert download.kwargs == {"target": "kakao", "scheduled": True}
        assert scheduler.get_job("download_job:1") is None and scheduler.get_job("discovery_job") is None
        assert {key: repository.get_setting(key) for key in keys} == before
    print("Internal refresh registration remains unique while user schedules and their stored settings stay intact")


async def main():
    await owned_login_recovers()
    await stale_response_cannot_undo_expiry_renewal()
    await failed_image_cannot_store_cookies()
    await readonly_request_replays_once()
    await mutation_is_not_replayed()
    await timeout_preserves_credentials_and_backoff()
    internal_job_survives_rescheduling()


asyncio.run(main())
print("All Kakao client and scheduler refresh integration checks passed")
