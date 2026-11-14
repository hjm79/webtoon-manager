"""Independent automation delay and notification quiet-hour saves preserve each other."""
import asyncio

import httpx

from app import db, report_dispatch

db.get_connection()
import app.main as main


async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        default = (await client.get("/api/settings/report")).json()
        assert default == {"quiet_hours": [], "batch_minutes": report_dispatch.DEFAULT_BATCH_MINUTES}
        night = {"start_hour": 23, "start_minute": 15, "end_hour": 6, "end_minute": 30, "days": ["mon", "wed"]}
        full = await client.post("/api/settings/report", json={"quiet_hours": [night], "batch_minutes": 7})
        assert full.status_code == 200 and full.json() == {"quiet_hours": [night], "batch_minutes": 7}
        batch = await client.post("/api/settings/report", json={"batch_minutes": 3})
        assert batch.status_code == 200 and batch.json() == {"quiet_hours": [night], "batch_minutes": 3}
        quiet = await client.post("/api/settings/report", json={"quiet_hours": []})
        assert quiet.status_code == 200 and quiet.json() == {"quiet_hours": [], "batch_minutes": 3}
        unchanged = await client.post("/api/settings/report", json={})
        assert unchanged.status_code == 200 and unchanged.json() == quiet.json()
        assert (await client.get("/api/settings/report")).json() == quiet.json()
        assert (await client.post("/api/settings/report", json={"batch_minutes": -1})).status_code == 422
        assert (await client.get("/api/settings/report")).json() == quiet.json()
        assert (await client.post("/api/settings/report", json={"quiet_hours": [night], "batch_minutes": -1})).status_code == 422
        assert (await client.get("/api/settings/report")).json() == quiet.json(), "Rejected full save must not partially change quiet hours"
        assert (await client.post("/api/settings/report", json={"quiet_hours": [{**night, "start_hour": 25}]})).status_code == 422
        assert (await client.get("/api/settings/report")).json() == quiet.json()
        with db.write_transaction() as conn:
            conn.execute("CREATE TEMP TRIGGER reject_batch BEFORE INSERT ON settings WHEN NEW.key='report_batch_minutes' BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False), base_url="http://test") as failing:
                assert (await failing.post("/api/settings/report", json={"quiet_hours": [night], "batch_minutes": 8})).status_code == 500
            assert (await client.get("/api/settings/report")).json() == quiet.json(), "A database failure must roll back the entire save"
        finally:
            with db.write_transaction() as conn:
                conn.execute("DROP TRIGGER reject_batch")
        assert (await client.post("/api/settings/report", json={"batch_minutes": 0})).json() == {"quiet_hours": [], "batch_minutes": 0}
    print("Partial report/automation saves, explicit reset, validation and legacy full payload OK")


asyncio.run(check())
