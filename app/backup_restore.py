"""
백업 복원 + 복원한 환경 점검.

백업에는 DB 내용(구독/제외/작가/이력/아카이빙 대상/설정 등)만 들어 있다. 다운로드한 파일, 보관 폴더, 로그인 쿠키, 디스코드 비밀값, rclone 설정은
들어 있지 않아서, **완전히 새로운 컨테이너에 복원하면** 데이터는 돌아오지만 환경은 비어 있다. 이 모듈은 복원 직후 그 차이를 점검해서
사용자에게 "다시 해야 할 일"을 알려 준다.

- 환경에 따라 달라지는 다운로드 폴더 설정이 이 환경에 없는 경로면 기본 폴더로 되돌린다(없는 경로에 받으려다 엉뚱한 곳에 쌓이는 걸 막는다).
- 구독 중인 작품의 폴더가 없으면 알린다 — 특히 카카오페이지는 폴더가 없으면 처음부터 전부 받으므로, 볼륨이 안 붙은 채로 스케줄이 돌면 위험하다.
- rclone 원격이나 보관 폴더를 쓰는 아카이빙 대상이 있는데 그 설정/폴더가 없으면 알린다.
- 다시 입력해야 하는 비밀값(디스코드 웹훅, 봇 토큰, 카카오페이지 로그인 쿠키, 네이버 성인 쿠키 파일)을 알려 준다.
"""

import logging
from pathlib import Path

from app import discord_config, download_roots, kakao_page_auth, kakao_page_download, repository
from app.cookie_loader import get_adult_cookies
from app.file_utils import remove_forbidden_str, remove_forbidden_str_kakao

_MAX_EXAMPLES = 3  # 경고에 예로 보여줄 작품 제목 수
_PLATFORM_LABELS = {"naver": "네이버", "kakao": "카카오페이지"}
log = logging.getLogger(__name__)


def restore_backup(data, settings) -> dict:
    """백업을 복원하고 환경을 점검한 결과를 돌려준다: {"status", "warnings": [...], "reenter": [...]}.
    백업이 올바르지 않으면 ValueError(데이터는 그대로)."""
    data, warnings = _guard_integrated_download_roots(data, settings)
    repository.restore_all(data)
    mount_problems = {}
    for platform in _PLATFORM_LABELS:
        try:
            problem = download_roots.download_root_problem(platform, settings)
        except Exception:
            log.exception("복원 뒤 다운로드 저장소 확인 실패 (%s)", platform)
            problem = "다운로드 폴더와 마운트 상태를 확인해 주세요."
        if problem:
            mount_problems[platform] = problem
    report = {
        "status": "restored_with_warnings" if mount_problems else "restored",
        "warnings": warnings, "reenter": [], "download_mount_problems": mount_problems,
    }
    for platform, problem in mount_problems.items():
        warnings.append(f"{_PLATFORM_LABELS[platform]} 다운로드 저장소 확인 필요. {problem}")
    if mount_problems:
        warnings.append("다운로드 대기열은 보존했습니다. 저장소를 복구하면 보류한 작업을 다시 시도합니다.")
    try:
        environment = check_environment(settings, fix=True, blocked_downloads=set(mount_problems))
        warnings.extend(environment["warnings"])
        report["reenter"] = environment["reenter"]
    except Exception:
        record_post_restore_failure(report, "다운로드·보관 환경 점검")
    return report


def _matches_viewer_download_root(value: str | None, expected: Path) -> bool:
    # Preserve the configured path while its volume is unavailable. The readiness
    # check after committing the restore handles the actual mount and symlink state.
    return bool(value) and Path(value) == expected


def _guard_integrated_download_roots(data, settings) -> tuple[dict, list[str]]:
    naver = download_roots.viewer_download_root("naver", settings)
    rows = data.get("settings") if isinstance(data, dict) else None
    if naver is None or not isinstance(rows, list):
        return data, []

    roots = {
        download_roots.NAVER_ROOT_SETTING_KEY: (naver, "네이버"),
        kakao_page_download.DOWNLOAD_ROOT_SETTING_KEY: (download_roots.viewer_download_root("kakao", settings), "카카오페이지"),
    }
    warnings = []
    restored_rows = []
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        value = row.get("value") if isinstance(row, dict) else None
        if isinstance(key, str) and key in roots and isinstance(value, str) and value:
            expected, label = roots[key]
            if not _matches_viewer_download_root(value, expected):
                current = repository.get_setting(key)
                keep_current = _matches_viewer_download_root(current, expected)
                row = {**row, "value": current if keep_current else None}
                retained = "기존 설정을 유지했습니다." if keep_current else "플랫폼 기본 경로를 사용하도록 설정했습니다."
                warnings.append(f"백업의 {label} 다운로드 폴더({value})가 통합 뷰어 연결 경로({expected})와 맞지 않아 {retained}")
        restored_rows.append(row)
    return ({**data, "settings": restored_rows} if warnings else data), warnings


def record_post_restore_failure(report: dict, step: str) -> None:
    log.exception("백업 데이터 저장 후 후속 작업 실패 (%s)", step)
    report["status"] = "restored_with_warnings"
    report["warnings"].append(f"백업 데이터는 저장되었습니다. {step} 작업을 완료하지 못했습니다.")


def check_environment(settings, *, fix: bool = False, blocked_downloads: set[str] | None = None) -> dict:
    """지금 환경이 복원된 데이터를 쓸 수 있는 상태인지 점검한다. fix=True면 이 환경에 없는 다운로드 폴더 설정을 기본값으로 되돌린다."""
    warnings: list[str] = []
    if fix and download_roots.viewer_download_root("naver", settings) is None:
        warnings += _reset_missing_download_roots()
    warnings += _missing_download_folder_warnings(settings, blocked_downloads or set())
    warnings += _archive_environment_warnings(settings)
    return {"warnings": warnings, "reenter": _reenter_items(settings)}


def _reset_missing_download_roots() -> list[str]:
    warnings = []
    for key, label in ((download_roots.NAVER_ROOT_SETTING_KEY, "네이버"), (kakao_page_download.DOWNLOAD_ROOT_SETTING_KEY, "카카오페이지")):
        value = repository.get_setting(key)
        if value and not Path(value).is_dir():
            repository.set_setting(key, None)
            warnings.append(
                f"{label} 다운로드 폴더({value})가 이 환경에 없어서 기본 폴더로 되돌렸습니다. "
                "필요하면 설정 > 다운로드 폴더에서 다시 고르세요."
            )
    return warnings


def _examples(titles: list[str]) -> str:
    shown = ", ".join(titles[:_MAX_EXAMPLES])
    return f"{shown} 등" if len(titles) > _MAX_EXAMPLES else shown


def _missing_download_folder_warnings(settings, blocked_downloads: set[str]) -> list[str]:
    warnings = []
    kakao_root = Path(download_roots.kakao_root(settings))
    kakao_missing = [] if "kakao" in blocked_downloads else [
        wt["title"] for wt in repository.list_kakao_webtoons_by_status(repository.STATUS_ACTIVE)
        if not (kakao_root / remove_forbidden_str_kakao(wt["title"])).is_dir()
    ]
    if kakao_missing:
        warnings.append(
            f"카카오페이지 구독 작품 {len(kakao_missing)}개의 폴더가 없습니다({_examples(kakao_missing)}). "
            "자동 다운로드가 그 작품을 처음부터 전부 받습니다. 이미 받아 둔 파일이 있다면 다운로드 폴더(볼륨)가 제대로 연결돼 있는지 "
            "스케줄이 돌기 전에 확인하세요."
        )
    naver_root = Path(download_roots.naver_root(settings))
    naver_missing = [] if "naver" in blocked_downloads else [
        wt.title for wt in repository.list_by_status(repository.STATUS_ACTIVE)
        if wt.last_downloaded_no > 0 and not (naver_root / remove_forbidden_str(wt.title)).is_dir()
    ]
    if naver_missing:
        warnings.append(
            f"네이버 구독 작품 {len(naver_missing)}개의 폴더가 없습니다({_examples(naver_missing)}). "
            "기록상 받은 회차는 다시 받지 않지만 파일이 없는 상태입니다. 다운로드 폴더(볼륨)가 제대로 연결돼 있는지 확인하세요."
        )
    return warnings


def _archive_environment_warnings(settings) -> list[str]:
    warnings = []
    targets = repository.list_archive_targets()
    uses_rclone = [t for t in targets if "rclone" in (t.dest_type, t.source_dest_type)]
    if uses_rclone and not Path(settings.rclone_config_path or "").is_file():
        warnings.append(
            f"rclone 원격을 쓰는 아카이빙 대상 {len(uses_rclone)}개가 있는데 이 환경에 rclone 설정 파일이 없습니다. "
            "RCLONE_CONFIG_HOST_PATH 폴더에 rclone.conf를 넣어 주세요(없으면 그 대상은 옮겨지지 않습니다)."
        )
    uses_local = [t for t in targets if "local" in (t.dest_type, t.source_dest_type)]
    if uses_local and not (settings.archive_root and Path(settings.archive_root).is_dir()):
        warnings.append(
            f"로컬 보관 폴더를 쓰는 아카이빙 대상 {len(uses_local)}개가 있는데 이 환경에 보관 폴더가 연결돼 있지 않습니다. "
            "WEBTOON_ARCHIVE_HOST_PATH를 확인하세요."
        )
    return warnings


def _reenter_items(settings) -> list[str]:
    """백업에 들어 있지 않아서 새 환경에서 다시 준비해야 하는 것들(이미 준비돼 있으면 빼고)."""
    items = []
    if not discord_config.get_webhook_url():
        items.append("디스코드 웹훅 주소 — 알림과 리포트를 받으려면 (설정 > 디스코드 설정)")
    if not (discord_config.get_bot_token() and discord_config.get_notify_channel_id()):
        items.append("디스코드 봇 토큰과 채널 ID — 완결 알림의 버튼을 쓰려면 (설정 > 디스코드 설정, 선택)")
    if repository.get_setting("kakao_webtoons_enabled") == "1" and not kakao_page_auth.load_cookies():
        items.append("카카오페이지 로그인 쿠키 — 카카오페이지 다운로드를 쓰려면 (설정 > 카카오웹툰 관리)")
    if any(wt.is_adult for wt in repository.list_by_status(repository.STATUS_ACTIVE)) and not get_adult_cookies(settings.cookie_file_path):
        items.append("네이버 성인 인증 쿠키 파일 — 구독 중인 성인 웹툰을 받으려면 (COOKIE_DIR_HOST_PATH 폴더)")
    return items
