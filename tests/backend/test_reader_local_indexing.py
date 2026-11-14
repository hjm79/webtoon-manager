"""Viewer lookup 404 distinguishes unindexed local archives from an empty title."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from app import db, download_roots, reader_files, repository, viewer_scan
from app.config import get_settings
from app.file_utils import remove_forbidden_str_kakao
db.get_connection()
import app.main as main
from app.api import reader


class Session:
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


async def run():
    naver = Path(tempfile.mkdtemp())
    kakao = Path(tempfile.mkdtemp())
    repository.set_setting("webtoon_server_url", "http://viewer")
    repository.set_setting(download_roots.NAVER_ROOT_SETTING_KEY, str(naver))
    repository.set_setting("kakao_download_root", str(kakao))
    title = "받은 작품"
    folder = naver / title
    folder.mkdir()
    for name in ("0001.zip", "0002.CBZ"):
        (folder / name).write_bytes(b"completed")
    (folder / "0003.zip.part").write_bytes(b"partial")
    (folder / "0004.zip").write_bytes(b"")
    (folder / "nested.zip").mkdir()
    (folder / "nested.zip" / "0005.zip").write_bytes(b"not recursive")
    linked = folder / "linked.zip"
    linked.symlink_to(folder / "0001.zip")
    calls = []
    async def missing(*args): return 404, None
    original_to_thread = asyncio.to_thread
    async def thread(func, *args, **kwargs):
        calls.append(func)
        return await original_to_thread(func, *args, **kwargs)
    with patch.object(reader.aiohttp, "ClientSession", Session), patch.object(reader, "_lookup", missing), \
         patch.object(reader.asyncio, "to_thread", thread), patch.object(viewer_scan, "request") as scan:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            result = (await client.get("/api/reader/status", params={"title": title, "platform": "naver"})).json()
            assert result["status"] == "indexing" and result["downloaded_count"] == 2 and result["url"] is None, result
            assert "2개" in result["message"]
            assert reader_files.completed_count in calls, "Directory reads must run in to_thread"
            empty = (await client.get("/api/reader/status", params={"title": "아직 없음"})).json()
            assert empty["status"] == "not_downloaded" and empty["downloaded_count"] == 0
            ktitle = "작품: 카카오"
            kfolder = kakao / remove_forbidden_str_kakao(ktitle)
            kfolder.mkdir()
            (kfolder / "0001.zip").write_bytes(b"completed")
            result = (await client.get("/api/reader/status", params={"title": ktitle, "platform": "kakao"})).json()
            assert result["status"] == "indexing" and result["downloaded_count"] == 1
            # Polling readiness is read-only and cannot repeatedly trigger a rescan.
            await client.get("/api/reader/status", params={"title": title})
            scan.assert_not_called()
    # Positive local evidence wins over an optional remote or unreadable archive.
    settings = NS(download_root=str(naver), archive_root=str(naver / "optional-remote-archive"))
    def mount(path):
        return {"status": "remote"} if path.startswith(settings.archive_root) else None
    with patch.object(reader_files.download_storage, "_mount_problem", mount):
        assert reader_files.completed_count(settings, title, "naver") == 2
    settings.archive_root = tempfile.mkdtemp()
    (Path(settings.archive_root) / title).mkdir()
    original_scandir = reader_files.os.scandir
    def unreadable_archive(path):
        if str(path).startswith(settings.archive_root):
            raise PermissionError("archive unavailable")
        return original_scandir(path)
    with patch.object(reader_files.os, "scandir", unreadable_archive):
        assert reader_files.completed_count(settings, title, "naver") == 2

    # A missing configured root is unknown, while an absent title inside a present root is empty.
    repository.set_setting(download_roots.NAVER_ROOT_SETTING_KEY, str(naver / "missing-root"))
    assert reader_files.completed_count(NS(download_root=str(naver), archive_root=""), title, "naver") is None
    repository.set_setting(download_roots.NAVER_ROOT_SETTING_KEY, str(naver))
    assert reader_files.completed_count(NS(download_root=str(naver), archive_root=""), "없는 제목", "naver") == 0
    with patch.object(reader_files.download_storage, "_mount_problem", return_value={"status": "not_mounted"}), \
         patch.object(reader_files.os, "scandir") as not_mounted_scan:
        assert reader_files.completed_count(settings, title, "naver") is None
        not_mounted_scan.assert_not_called()

    outside = Path(tempfile.mkdtemp())
    (outside / "0001.zip").write_bytes(b"outside")
    (naver / "탈출").symlink_to(outside, target_is_directory=True)
    assert reader_files.completed_count(get_settings(), "탈출", "naver") is None
    assert reader_files.completed_count(get_settings(), "..", "naver") == 0
    with patch.object(reader_files.download_storage, "_mount_problem", return_value={"status": "remote"}), \
         patch.object(reader_files.os, "scandir") as scandir:
        assert reader_files.completed_count(get_settings(), title, "naver") is None
        scandir.assert_not_called()


asyncio.run(run())
print("Local completed archives return indexing; empty, symlink and remote paths remain distinct; GET never scans")
