"""Read exact chapter movement identities written by the manager's local archiver."""
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("webtoon-server")


def load(chapter_path: Path, siblings: set[str]) -> dict | None:
    digest = hashlib.sha256(chapter_path.name.encode()).hexdigest()
    marker_name = f".webtoon-origin-{digest}.json"
    if marker_name not in siblings:
        return None
    marker = chapter_path.with_name(marker_name)
    try:
        if marker.is_symlink() or not marker.is_file() or chapter_path.is_symlink():
            return None
        with marker.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("destination") != chapter_path.name:
            return None
        platform, title_id = data.get("platform"), data.get("title_id")
        ref, filename = data.get("series_ref"), data.get("filename")
        if platform not in {"naver", "kakao"} or not _provider_id(title_id):
            return None
        if not isinstance(ref, str) or not _safe_relative(ref) or not isinstance(filename, str):
            return None
        if not _safe_relative(filename) or "/" in filename or Path(filename).suffix.lower() not in {".zip", ".cbz"}:
            return None
        stat = chapter_path.stat()
        if stat.st_size <= 0 or stat.st_size != data.get("size") or stat.st_mtime_ns != data.get("mtime_ns"):
            return None
        return {"platform": platform, "title_id": title_id, "series_ref": ref, "filename": filename}
    except (OSError, TypeError, ValueError):
        log.warning("보관 회차 출처 정보를 읽지 못했습니다: %s", marker)
        return None


def _provider_id(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 32 and value.isascii() and value.isdecimal()


def _safe_relative(value: str) -> bool:
    if not value or "\\" in value or "\0" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts) and not PurePosixPath(value).is_absolute()


def from_official_url(url: str) -> tuple[str, str] | None:
    """Existing archive folders may have an official work URL in their info.xml."""
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port is not None:
            return None
        if parts.hostname == "comic.naver.com" and parts.path.rstrip("/") == "/webtoon/list":
            ids = parse_qs(parts.query).get("titleId", [])
            if len(ids) == 1 and _provider_id(ids[0]):
                return "naver", ids[0]
        if parts.hostname == "page.kakao.com":
            path = parts.path.strip("/").split("/")
            if len(path) == 2 and path[0] == "content" and _provider_id(path[1]):
                return "kakao", path[1]
    except ValueError:
        pass
    return None
