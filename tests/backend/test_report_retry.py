"""A webhook acknowledges each delivered part; failures retain their batch across ticks."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import db, discord_config, job_status, list_alert, report_dispatch, report_seen, repository
from app.constants import DISCORD_MESSAGE_CHUNK_LIMIT
from app.scheduler import report_job, report_tick


class Response:
    def __init__(self, status):
        self.status = status

    async def text(self):
        return "test failure"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class Transport:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.chunks = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def post(self, _url, *, json, allow_redirects):
        assert not allow_redirects
        self.chunks.append(json["content"])
        return Response(next(self.statuses))


NOW = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
PARTS = [letter * DISCORD_MESSAGE_CHUNK_LIMIT for letter in "ABC"]


def add_download(title, when):
    repository.add_episode_history(title, title, 1, "1화", "success")
    with db.write_transaction() as conn:
        conn.execute("UPDATE episode_history SET downloaded_at = ? WHERE title_id = ?", (when.isoformat(), title))
    report_dispatch.note_download_finished(when)


async def retry_batch():
    since = (NOW - timedelta(hours=1)).isoformat()
    repository.set_setting(report_dispatch.LAST_SENT_KEY, since)
    repository.set_setting(list_alert.SETTING_ENABLED, "0")
    report_dispatch.set_batch_minutes(0)
    add_download("first", NOW)
    transport = Transport([204, 500, 204, 204, 204])
    with patch.object(report_job.aiohttp, "ClientSession", return_value=transport), patch.object(
        report_job, "_build_message", new=AsyncMock(side_effect=["\n".join(PARTS), "second"]),
    ) as build:
        await report_tick.run_report_tick(NOW)
        assert transport.chunks == PARTS[:2]
        state = report_dispatch.load_state()
        assert state["cutoff"] == NOW.isoformat() and state["delivery"]["next_chunk"] == 1
        assert job_status.snapshot()["report"]["status"] == "error"
        assert repository.get_setting(report_dispatch.LAST_SENT_KEY) == since
        assert repository.get_setting(report_seen.SETTING_KEY) is None

        second = NOW + timedelta(seconds=30)
        add_download("second", second)
        await report_tick.run_report_tick(second)
        assert transport.chunks == PARTS[:2]
        assert report_dispatch.load_state()["cutoff"] == NOW.isoformat()

        # The local partially delivered batch survives a restore of newer history.
        restored_cursor = (NOW + timedelta(seconds=15)).isoformat()
        repository.set_setting(report_dispatch.LAST_SENT_KEY, restored_cursor)
        await report_tick.run_report_tick(NOW + timedelta(minutes=1))
        assert transport.chunks == [PARTS[0], PARTS[1], PARTS[1], PARTS[2]]
        assert build.await_count == 1, "Retry must use the persisted message"
        assert repository.get_setting(report_dispatch.LAST_SENT_KEY) == restored_cursor
        assert report_dispatch.load_state()["last_download_at"] == second.isoformat()

        await report_tick.run_report_tick(NOW + timedelta(minutes=2))
        assert transport.chunks[-1] == "second" and build.await_count == 2
        assert repository.get_setting(report_dispatch.LAST_SENT_KEY) == second.isoformat()
        assert report_dispatch.load_state()["cutoff"] is None


async def retry_standalone():
    repository.set_setting(list_alert.SETTING_ENABLED, "1")
    list_alert.set_time("09:00")
    transport = Transport([204, 429, 204])
    with patch.object(report_job.aiohttp, "ClientSession", return_value=transport), patch.object(
        report_job, "collect_new_title_section", new=AsyncMock(return_value=([("1", "제목")], [])),
    ) as collect_new, patch.object(
        report_job, "collect_new_episode_sections", new=AsyncMock(return_value=([], [])),
    ), patch.object(report_job, "build_list_alert_message", return_value="\n".join(PARTS[:2])):
        await report_tick.run_report_tick(NOW)
        assert not list_alert.is_done(NOW) and not report_seen.is_new_announced("naver", "1")
        assert report_dispatch.load_list_delivery()["next_chunk"] == 1
        await report_tick.run_report_tick(NOW + timedelta(minutes=9))
        assert transport.chunks == PARTS[:2]
        await report_tick.run_report_tick(NOW + timedelta(minutes=10))
        assert transport.chunks == [PARTS[0], PARTS[1], PARTS[1]]
        assert collect_new.await_count == 1 and report_seen.is_new_announced("naver", "1")
        assert list_alert.is_done(NOW) and not report_dispatch.load_list_delivery()


async def main():
    db.get_connection()
    discord_config.set_webhook_url("https://discord.com/api/webhooks/1/unit-test")
    await retry_batch()
    await retry_standalone()


asyncio.run(main())
