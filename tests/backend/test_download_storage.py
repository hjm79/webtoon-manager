"""Download filesystem capacity, safe draft reads and bounded failed-mount IO."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import patch

import httpx

from app import db, download_storage as storage, repository
from app.config import get_settings

db.get_connection()
import app.main as main


def usage(*, read_only=False):
    return SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=1000, f_bfree=400,
                           f_bavail=350, f_flag=getattr(os, "ST_RDONLY", 1) if read_only else 0)


def clear_cache():
    with storage._lock:
        storage._cache.clear()


def unit_cases():
    base = Path(get_settings().download_root)
    with patch.object(storage, "_mount_for", return_value=None):
        result = storage._read_capacity(str(base))
        assert result["status"] == "ok" and result["total_bytes"] > 0
        with patch.object(storage.os, "statvfs", return_value=usage(read_only=True)):
            result = storage._read_capacity(str(base))
        assert result["total_bytes"] == 4096000 and result["used_bytes"] == 2457600
        assert result["available_bytes"] == 1433600 and result["read_only"] is True
        assert result["filesystem_id"].startswith("device:")
        with patch.object(storage.os, "statvfs", side_effect=AssertionError("No parent fallback")):
            assert storage._read_capacity(str(base / "missing"))["status"] == "missing"
        with patch.object(Path, "stat", side_effect=PermissionError):
            assert storage._read_capacity(str(base))["status"] == "permission_denied"
        with patch.object(storage.os, "statvfs", side_effect=OSError):
            assert storage._read_capacity(str(base))["status"] == "unavailable"
    for kind in ["fuse.rclone", "nfs4"]:
        with patch.object(storage, "_mount_for", return_value={"point": "/webtoon_download", "type": kind}), \
             patch.object(Path, "resolve", side_effect=AssertionError("Do not touch unavailable mounts")):
            assert storage._read_capacity("/webtoon_download/naver")["status"] == "remote"
    with patch.object(storage, "_mount_for", return_value={"point": "/", "type": "overlay"}), \
         patch.object(storage.os, "statvfs", side_effect=AssertionError("Do not report container overlay capacity")):
        assert storage._read_capacity(str(base))["status"] == "not_mounted"
    mountinfo = "11 1 0:1 / / rw - overlay overlay rw\n12 11 259:3 /downloads /webtoon_download rw - ext4 /dev/nvme0n1p1 rw\n13 12 8:1 / /webtoon_download/other\\040disk ro - ext4 /dev/sda1 ro\n"
    with patch.object(Path, "read_text", return_value=mountinfo):
        assert storage._mount_for("/webtoon_download/naver")["type"] == "ext4"
        assert storage._mount_for("/webtoon_download/other disk/naver")["point"] == "/webtoon_download/other disk"
        assert storage._mount_for("/webtoon_download_evil")["type"] == "overlay"
    print("Filesystem bytes, reserved space, read-only, failure and mount guards OK")


async def api_cases():
    base = Path(get_settings().download_root)
    naver, kakao = base / "naver", base / "kakao"
    naver.mkdir(); kakao.mkdir()
    repository.set_setting("naver_download_root", str(naver))
    repository.set_setting("kakao_download_root", str(kakao))
    clear_cache()
    with patch.object(storage, "_mount_for", return_value=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            before = (repository.get_setting("naver_download_root"), repository.get_setting("kakao_download_root"))
            response = await client.get("/api/settings/download-roots")
            state = response.json()
            assert response.status_code == 200
            assert state["naver"]["storage"]["status"] == "ok"
            assert state["naver"]["storage"]["filesystem_id"] == state["kakao"]["storage"]["filesystem_id"]
            current = (await client.get("/api/settings/download-storage", params={"kind": "kakao"})).json()["storage"]
            assert current["checked_path"] == str(kakao.resolve())
            draft = (await client.get("/api/settings/download-storage", params={"kind": "naver", "path": "kakao"})).json()["storage"]
            assert draft["checked_path"] == str(kakao.resolve())
            default = (await client.get("/api/settings/download-storage", params={"path": ""})).json()["storage"]
            assert default["checked_path"] == str(base.resolve())
            missing = (await client.get("/api/settings/download-storage", params={"path": "not-created"})).json()["storage"]
            assert missing["status"] == "missing" and not (base / "not-created").exists()
            assert before == (repository.get_setting("naver_download_root"), repository.get_setting("kakao_download_root"))
            outside = base.parent / "outside"
            outside.mkdir()
            (base / "escape").symlink_to(outside, target_is_directory=True)
            for attack in ["../outside", str(outside), "escape", "escape/new", "\x00"]:
                response = await client.get("/api/settings/download-storage", params={"path": attack})
                assert response.status_code == 400, (attack, response.text)
            with patch.object(storage, "_mount_for", return_value={"point": "/", "type": "overlay"}):
                response = await client.get("/api/settings/download-storage", params={"path": "escape/overlay-check"})
                assert response.status_code == 400
            assert (await client.get("/api/settings/download-storage", params={"kind": "bad"})).status_code == 422
            response = await client.post("/api/settings/download-roots", json={"naver": str(outside), "kakao": ""})
            assert response.status_code == 200
            state = response.json()
            assert state["naver"]["effective"] == str(outside) and state["kakao"]["effective"] == str(outside)
            assert state["naver"]["storage"] == state["kakao"]["storage"]
            assert (await client.get("/api/settings/download-storage", params={"kind": "naver"})).json()["storage"]["status"] == "ok"
    clear_cache()
    async def different(path, **_kwargs):
        result = storage._state(path, "ok")
        result.update(filesystem_id="device:" + Path(path).name, total_bytes=100, used_bytes=60, available_bytes=40)
        return result
    repository.set_setting("naver_download_root", str(naver))
    repository.set_setting("kakao_download_root", str(kakao))
    with patch.object(storage, "capacity", different):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            state = (await client.get("/api/settings/download-roots")).json()
            assert state["naver"]["storage"]["filesystem_id"] != state["kakao"]["storage"]["filesystem_id"]
    print("Saved/inherited/separate roots, safe draft queries and unchanged settings OK")


async def timeout_cases():
    clear_cache()
    release = threading.Event()
    entered = []
    def blocked(path, _allowed_root):
        entered.append(path)
        assert release.wait(2)
        return storage._state(path, "ok")
    try:
        with patch.object(storage, "_read_capacity", blocked), patch.object(storage, "_LOOKUP_SECONDS", 0.02):
            first, duplicate, second = await asyncio.gather(storage.capacity("/blocked-one"), storage.capacity("/blocked-one"), storage.capacity("/blocked-two"))
            assert first["status"] == duplicate["status"] == second["status"] == "timeout"
            assert sorted(entered) == ["/blocked-one", "/blocked-two"]
            assert (await storage.capacity("/blocked-three"))["status"] == "unavailable"
            assert len(entered) == 2
            release.set()
            futures = list(storage._pending.values())
            await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
    finally:
        release.set()
    print("Timeouts reuse in-flight OS work and bound outstanding queries to two OK")


if __name__ == "__main__":
    unit_cases()
    asyncio.run(api_cases())
    asyncio.run(timeout_cases())
