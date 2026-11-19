"""A refresh cooldown must retain the original queued work and its retry category."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from app import db, kakao_page_auth as auth, kakao_session_refresh as refresh, repository
from app.config import get_settings
from app.scheduler import kakao_download


async def check_refresh_outcome(result, expected_auth, expected_queue):
    cookies = [
        {"name": name, "value": f"QUEUE_{number}", "domain": ".kakao.com", "path": "/", "secure": True}
        for number, name in enumerate(auth.REQUIRED_COOKIES)
    ]
    generation = auth.save_cookies(cookies)
    assert auth.record_health(False, generation, "session_rejected", expected_cookies=cookies)
    title_id = "990111"
    if not repository.get_kakao_webtoon(title_id):
        repository.activate_scoped_subscription("kakao", title_id, "Queue fixture", "", "", {"mode": "selected"})
    item = repository.enqueue_download("kakao", title_id, "Queue fixture")[0]

    with (patch.object(refresh, "refresh_saved_session", new=AsyncMock(return_value=result)),
          patch.object(auth, "notify_if_needed", new_callable=AsyncMock) as notify):
        try:
            await kakao_download._download_kakao_subscriptions(
                get_settings(), [], only=[{"title_id": title_id, "title": "Queue fixture"}]
            )
        except auth.KakaoAuthPending as exc:
            assert exc.state == expected_auth and exc.reason == result["reason"]
            assert auth.defer_download(item["id"], exc)[0] == expected_queue
        else:
            raise AssertionError("Unverified credentials did not pause the download")

    notify.assert_awaited_once()
    assert notify.await_args.args[1] is (False if expected_auth == "reauth_required" else None)
    queued = repository.list_download_queue()[0]
    assert queued["id"] == item["id"] and queued["state"] == expected_queue
    if expected_queue == "retry_wait":
        assert queued["retry_at"] > time.time()
    repository.update_download_queue(item["id"])


async def main():
    db.get_connection()
    await check_refresh_outcome(
        {"state": "deferred", "reason": "refresh_backoff"}, "unknown", "retry_wait"
    )
    await check_refresh_outcome(
        {"state": "reauth_required", "reason": "refresh_rejected"}, "reauth_required", "blocked_auth"
    )
    print("Temporary cooldown preserves retry_wait; definitive rejection preserves blocked_auth")


asyncio.run(main())
