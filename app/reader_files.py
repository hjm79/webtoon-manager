"""Count completed local chapter archives without opening archives or walking subfolders."""
import os
from pathlib import Path

from app import download_roots, download_storage
from app.file_utils import remove_forbidden_str, remove_forbidden_str_kakao


def completed_count(settings, title: str, platform: str) -> int | None:
    safe = (remove_forbidden_str_kakao if platform == "kakao" else remove_forbidden_str)(title.strip())
    if safe in ("", ".", "..") or Path(safe).name != safe:
        return 0
    primary = download_roots.kakao_root(settings) if platform == "kakao" else download_roots.naver_root(settings)
    roots = [primary, getattr(settings, "archive_root", "")]
    checked = set()
    count = 0
    unknown = False
    for raw in roots:
        if not raw:
            continue
        root = Path(raw)
        if not root.is_absolute():
            unknown = True
            continue
        # Read mount metadata before accessing a configured root. Unknown roots
        # must not erase positive evidence from another local library.
        try:
            if download_storage._mount_problem(str(root)) is not None or root.is_symlink():
                unknown = True
                continue
            root = root.resolve()
            if not root.is_dir():
                unknown = True
                continue
            folder = root / safe
            if str(folder) in checked:
                continue
            checked.add(str(folder))
            if download_storage._mount_problem(str(folder)) is not None or folder.is_symlink():
                unknown = True
                continue
            try:
                with os.scandir(folder) as entries:
                    count += sum(1 for entry in entries if Path(entry.name).suffix.casefold() in (".zip", ".cbz")
                                 and entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_size > 0)
            except FileNotFoundError:
                continue  # The configured root exists; this title has no folder.
        except (OSError, RuntimeError):
            unknown = True
    return count if count > 0 or not unknown else None
