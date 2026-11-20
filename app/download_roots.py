"""
다운로드 폴더 해석 — 네이버와 카카오페이지를 서로 다른 폴더에 받을 수 있게, "지금 실제로 쓰는 폴더"를 한 곳에서 정한다.

- 통합 배포의 기본 경로는 뷰어에 연결된 DOWNLOAD_ROOT/naver와 DOWNLOAD_ROOT/kakao다.
- 독립 배포는 기존대로 네이버는 DOWNLOAD_ROOT, 카카오페이지는 네이버 다운로드 폴더를 기본값으로 쓴다.
- 아카이빙의 폴더/일괄 이동은 폴더를 이름표("archive" | "download" | "kakao_download")로 가리킨다. "download"는 네이버 폴더다.
  "download_base"는 컨테이너에 마운트된 다운로드 폴더 자체(DOWNLOAD_ROOT)로, 다운로드 폴더를 고르는 화면이 그 아래를 찾아볼 때 쓴다 —
  선택한 폴더를 따라 움직이지 않는 고정된 출발점이다.
  웹툰 유형 아카이빙 대상은 실행할 때마다 폴더를 새로 받아 쓰므로 폴더를 바꿔도 자동으로 따라간다(옮기는 건 사용자 몫).
"""

import os
from pathlib import Path

from app import download_storage, kakao_page_download, repository

NAVER_ROOT_SETTING_KEY = "naver_download_root"
LOCAL_ROOT_NAMES = ("archive", "download", "kakao_download", "download_base")


def viewer_download_root(platform: str, settings) -> Path | None:
    if getattr(settings, "viewer_internal_url", "").strip():
        return Path(settings.download_root) / platform
    return None


def naver_default(settings) -> str:
    return str(viewer_download_root("naver", settings) or settings.download_root)


def kakao_default(settings) -> str:
    return str(viewer_download_root("kakao", settings) or naver_root(settings))


def naver_root(settings) -> str:
    return repository.get_setting(NAVER_ROOT_SETTING_KEY) or naver_default(settings)


def kakao_root(settings) -> str:
    return kakao_page_download.effective_download_root(kakao_default(settings))


def download_root_problem(platform: str, settings, *, path: str | Path | None = None) -> str | None:
    """Check the integrated download directory before reading or writing media."""
    expected = viewer_download_root(platform, settings)
    if expected is None:
        return None
    if path is None:
        path = naver_root(settings) if platform == "naver" else kakao_root(settings)
    folder = Path(path)
    if folder != expected or not folder.is_absolute():
        return f"통합 뷰어에 연결된 다운로드 경로({expected})를 확인해주세요."
    try:
        mount = download_storage._mount_for(str(folder))
        base = expected.parent
        in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
        if in_container and (mount is None or not Path(mount["point"]).is_relative_to(base)):
            return f"다운로드 디스크가 마운트되지 않았습니다 ({base})."
        if download_storage._mount_problem(str(folder)) is not None:
            return f"다운로드 경로에 쓰기 가능한 로컬 디스크를 연결해주세요 ({folder})."
        if folder.resolve(strict=True) != base.resolve(strict=True) / expected.name or not folder.is_dir():
            return f"다운로드 경로가 실제 뷰어 폴더를 가리키지 않습니다 ({folder})."
        if os.statvfs(folder).f_flag & getattr(os, "ST_RDONLY", 1) or not os.access(folder, os.W_OK | os.X_OK):
            return f"다운로드 폴더에 쓸 수 없습니다 ({folder})."
    except (OSError, RuntimeError, ValueError):
        return f"다운로드 폴더에 접근할 수 없습니다 ({folder})."
    return None


def local_root_path(name: str, settings) -> str:
    """로컬 폴더 이름표 → 실제 경로."""
    if name == "archive":
        return settings.archive_root
    if name == "download_base":
        return settings.download_root
    return kakao_root(settings) if name == "kakao_download" else naver_root(settings)


def host_path_of(container_path: str, settings) -> str:
    """컨테이너 안 경로를 호스트 경로로 바꿔 보여준다(마운트된 다운로드 폴더 아래일 때만, 호스트 경로를 알 때만). 모르면 ""."""
    host = settings.webtoon_download_host_path.strip()
    base = settings.download_root.rstrip("/")
    if not host or not (container_path == base or container_path.startswith(base + "/")):
        return ""
    rest = container_path[len(base):].strip("/")
    if not rest:
        return host
    sep = "\\" if "\\" in host else "/"  # 윈도우 경로면 역슬래시로 이어 붙인다
    return host.rstrip("\\/") + sep + rest.replace("/", sep)
