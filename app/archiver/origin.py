"""Persist the exact source of local chapter moves for the integrated viewer."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from app.config import get_settings


def chapter_origin(src: Path, dest_dir: Path, title_id: str) -> dict | None:
    settings = get_settings()
    if not settings.viewer_internal_url or not settings.archive_root or not src.is_file() or src.is_symlink():
        return None
    platform = "kakao" if title_id.startswith("kakao_") else "naver"
    provider_id = title_id.removeprefix("kakao_")
    if not (provider_id.isascii() and provider_id.isdecimal() and len(provider_id) <= 32):
        return None
    platform_root = Path(settings.download_root).resolve() / platform
    archive_root = Path(settings.archive_root).resolve()
    source = src.resolve()
    if not source.is_relative_to(platform_root) or not dest_dir.resolve().is_relative_to(archive_root):
        return None
    return {
        "version": 1,
        "platform": platform,
        "title_id": provider_id,
        "series_ref": source.parent.relative_to(platform_root).as_posix(),
        "filename": src.name,
    }


def marker_path(destination: Path) -> Path:
    digest = hashlib.sha256(destination.name.encode()).hexdigest()
    return destination.parent / f".webtoon-origin-{digest}.json"


def fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def write_marker(marker: Path, content: bytes) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=marker.parent, prefix=".webtoon-origin-", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content)
            # The reader can run under a different container user.
            os.fchmod(stream.fileno(), 0o644)
        temp.replace(marker)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def encode_marker(origin: dict, destination: Path, stat: dict) -> bytes:
    return json.dumps({**origin, **stat, "destination": destination.name}, ensure_ascii=False).encode()
