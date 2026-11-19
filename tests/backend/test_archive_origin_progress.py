"""An actual archive move keeps marked and in-progress chapters attached to their source identity."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from app import archiver, db, repository
from app.archiver import cancel, origin
from app.config import get_settings
from viewer.app import db as viewer_db, profile_progress, scan

settings = get_settings()
db.get_connection()


def write_chapter(folder, filename):
    path = folder / filename
    with ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.jpg", b"page-two")
    return path


with TemporaryDirectory() as temp, patch.object(settings, "viewer_internal_url", "http://viewer:8000"):
    root = Path(settings.download_root)
    naver = root / "naver"
    archive_root = Path(settings.archive_root)
    source_folder = naver / "보관 이어읽기"
    source_folder.mkdir(parents=True)
    first_name, second_name = "0001 첫 회.zip", "0002 둘째 회.zip"
    first = write_chapter(source_folder, first_name)
    second = write_chapter(source_folder, second_name)
    (source_folder / "info.xml").write_text("<ComicInfo><Title>보관 이어읽기</Title></ComicInfo>")
    with patch.object(viewer_db, "DB_PATH", str(Path(temp) / "progress.db")):
        viewer_db.init_schema()
        profile_progress.init_schema()
        before, _ = scan._scan_series_at_path("naver", source_folder.name, str(source_folder))
        source_id = before["id"]
        original_first, original_second = [chapter["id"] for chapter in before["chapters"]]
        viewer_db.mark_chapters_read(source_id, [original_first])
        viewer_db.set_progress(source_id, original_second, 1, 1, page_offset=0.45)
        profile_progress.mark_chapters_read("one", source_id, [original_second])
        profile_progress.set_progress("one", source_id, original_first, 0, 0, page_offset=0.25)

        repository.upsert_new("99101", source_folder.name)
        repository.set_status("99101", repository.STATUS_UNSUBSCRIBED)
        repository.upsert_archive_target("99101", "읽던회차", enabled=True)
        repository.set_setting(archiver.settings._ON_FINISH_UNSUBSCRIBE_SETTING_KEY, "1")
        repository.add_pending_finish_archive("99101")

        moved = archiver.process_pending_finish_archives(settings.archive_root, str(naver), progress_callback=lambda _: cancel.request())
        dest_folder = archive_root / "읽던회차"
        assert moved == 1 and not first.exists() and second.is_file()
        assert repository.list_pending_finish_archive() == ["99101"]
        assert (source_folder / "info.xml").is_file()
        moved_first = dest_folder / first_name
        first_marker = origin.marker_path(moved_first)
        assert first_marker.is_file()

        # Resume after a process-equivalent cancel reset; only the still-pending chapter moves.
        cancel.reset()
        assert archiver.process_pending_finish_archives(settings.archive_root, str(naver)) == 1
        assert not repository.list_pending_finish_archive() and not source_folder.exists()
        dest, _ = scan._scan_series_at_path("archive", dest_folder.name, str(dest_folder))
        dest_id = dest["id"]
        new_first, new_second = [chapter["id"] for chapter in dest["chapters"]]
        assert dest["archive_identity"] == ("naver", "99101")
        assert viewer_db.get_read_chapter_ids(dest_id) == {new_first}
        assert viewer_db.get_progress(dest_id) == {"chapter_id": new_second, "chapter_index": 1, "page_index": 1, "page_offset": 0.45}
        assert profile_progress.get_read_chapter_ids("one", dest_id) == {new_second}
        assert profile_progress.get_progress("one", dest_id)["page_offset"] == 0.25
        assert profile_progress.get_progress("other", dest_id) is None
        assert viewer_db.get_progress(source_id)["chapter_id"] == original_second

        viewer_db.mark_chapters_unread(dest_id, [new_first])
        viewer_db.set_progress(dest_id, new_first, 0, 0, page_offset=0.9)
        scan._scan_series_at_path("archive", dest_folder.name, str(dest_folder))
        assert not viewer_db.get_read_chapter_ids(dest_id)
        assert viewer_db.get_progress(dest_id)["page_offset"] == 0.9

        # Re-using an old name and marker cannot assign the previous identity to a new file.
        moved_first.write_bytes(b"replacement")
        changed, _ = scan._scan_series_at_path("archive", dest_folder.name, str(dest_folder))
        assert changed["archive_identity"] is None

print("Archive cancellation/restart, exact Naver origin, and admin/profile offsets verified")
