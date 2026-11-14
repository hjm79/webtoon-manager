"""기다무 작업은 이미지/인증 실패를 실패로 기록하고 정상적인 대기는 성공으로 남긴다."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.scheduler import waitfree_job


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
DUE = [{"title_id": 123, "title": "격리 검증", "waitfree_next_at": None}]


async def run_case(*, result=None, failures=None, missing=False, raises=False):
    statuses, delays = [], []

    async def download(settings, collected_failures, **kwargs):
        assert kwargs["only"] == DUE and kwargs["now"] == NOW
        collected_failures.extend(failures or [])
        if raises:
            raise RuntimeError("fixture download failure")
        return [] if missing else [(DUE[0], result)]

    with patch.object(waitfree_job, "get_settings", return_value=SimpleNamespace()), \
            patch.object(waitfree_job, "_download_kakao_subscriptions", download), \
            patch.object(waitfree_job.repository, "get_kakao_webtoon", return_value=DUE[0]), \
            patch.object(waitfree_job.job_status, "start"), \
            patch.object(waitfree_job.job_status, "log_line"), \
            patch.object(waitfree_job.job_status, "finish", side_effect=lambda _, success: statuses.append(success)), \
            patch.object(waitfree_job.waitfree_sync, "delay", side_effect=lambda *args: delays.append(args)):
        await waitfree_job._run_waitfree_batch(DUE, NOW)
    return statuses, delays


async def main():
    failed_cases = [
        {"result": SimpleNamespace(error=None, failed=3)},
        {"result": SimpleNamespace(error=None, failed=0)},
        {"result": SimpleNamespace(error="HTTP 429", failed=None)},
        {"result": None},
        {"missing": True, "failures": [{"title_name": "격리 검증", "episode_no": None, "subtitle": "로그인 필요"}]},
        {"missing": True},
        {"raises": True},
        {"result": SimpleNamespace(error=None, failed=None), "failures": [{"title_name": "격리 검증", "episode_no": 1, "subtitle": "배치 실패"}]},
    ]
    for case in failed_cases:
        statuses, delays = await run_case(**case)
        assert statuses == [False], (case, statuses)
        assert delays == [(123, waitfree_job.waitfree_sync.ERROR_MINUTES, NOW)], (case, delays)

    for reason in ("caught_up", "waiting_for_waitfree", "ticket_needed"):
        statuses, delays = await run_case(result=SimpleNamespace(error=None, failed=None, reason=reason))
        assert statuses == [True] and delays == [], (reason, statuses, delays)

    print("waitfree failure classification: 8 failure cases and 3 normal no-download cases OK")


asyncio.run(main())
