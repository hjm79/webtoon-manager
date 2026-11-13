"""New chapters become readable before the notification batching window ends."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import db, repository, report_dispatch, viewer_scan
db.get_connection()
from app.scheduler import report_tick


async def run():
    started = datetime.now(timezone.utc) - timedelta(seconds=20)
    finished = started + timedelta(seconds=10)
    repository.set_setting("webtoon_server_url", "http://viewer")
    viewer_scan.set_config(enabled=True, token="test-only-integration")
    report_dispatch.set_batch_minutes(10)
    report_dispatch.note_download_finished(finished, started_at=started.isoformat())
    repository.add_episode_history("1", "작품", 1, "1화", "success")
    # The event above occurs after our controlled cutoff, so insert a bounded historical event.
    from app.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE episode_history SET downloaded_at=?", ((finished - timedelta(seconds=1)).isoformat(),))
    conn.commit()
    requested = []
    original = viewer_scan.request

    def request(when):
        requested.append(when)
        original(when)

    async def advance(now, scan):
        assert viewer_scan.has_work()
        scan.advanced = True

    with patch.object(viewer_scan, "request", request), patch.object(report_tick, "_advance_scan", advance), patch.object(report_tick, "_list_alert_step", return_value=None):
        await report_tick.run_report_tick(finished + timedelta(seconds=1))
        assert requested == [finished]
        assert report_dispatch.load_state()["cutoff"] is None
        assert viewer_scan.load_state()["requested_at"] == finished.isoformat()
        await report_tick.run_report_tick(finished + timedelta(seconds=30))
        assert requested == [finished], "An unchanged batch must not request a second scan"
        report_tick._request_scan_for_batch(started.isoformat())
        assert viewer_scan.load_state()["watermark"] == finished.isoformat(), "The scan watermark must never move backward"


asyncio.run(run())
print("Viewer scan is independent from report delay and preserves monotonic scan watermark")
