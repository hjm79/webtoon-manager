"""Keep viewer transport addresses separate from browser-facing paths."""
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.config import get_settings

_PUBLIC_PATH = re.compile(r"(?:/[A-Za-z0-9_-]+)+\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def integrated() -> bool:
    return bool(get_settings().viewer_internal_url.strip())


def internal_base(fallback: str | None = None) -> str:
    raw = get_settings().viewer_internal_url.strip()
    if not raw:
        return (fallback or "").rstrip("/")
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(ord(c) <= 32 for c in raw)):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValueError("통합 뷰어 연결 주소가 올바르지 않습니다.") from None
    return raw.rstrip("/")


def public_path() -> str:
    path = get_settings().viewer_public_path.rstrip("/")
    if not _PUBLIC_PATH.fullmatch(path):
        raise ValueError("통합 뷰어 공개 경로가 올바르지 않습니다.")
    return path


def public_base(fallback: str | None = None) -> str:
    return public_path() if integrated() else (fallback or "").rstrip("/")


def reader_url(series_id: str, chapter_id: str | None = None) -> str | None:
    if not isinstance(series_id, str) or not _ID.fullmatch(series_id):
        return None
    params = {"series": series_id}
    if isinstance(chapter_id, str) and _ID.fullmatch(chapter_id):
        params["chapter"] = chapter_id
    return public_path() + "/reader.html?" + urlencode(params)


def report_reader_url(candidate: str | None, series_id: str, chapter_id: str | None = None) -> str | None:
    """Use the viewer's public origin for reports without copying its query or internal address."""
    path = reader_url(series_id, chapter_id)
    if not path or not isinstance(candidate, str):
        return path
    try:
        parsed = urlsplit(candidate)
        upstream = urlsplit(internal_base())
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.hostname == upstream.hostname
                or parsed.path != public_path() + "/reader.html"
                or any(ord(c) <= 32 for c in candidate)):
            return path
        parsed.port
    except ValueError:
        return path
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")) + path
