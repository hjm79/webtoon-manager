"""
카카오페이지 "받은 회차 기록" — 이 앱이 받았거나 폴더에서 확인한 회차 번호를 작품마다 따로 기억한다.

왜 필요한가: 아카이빙은 마지막 회차 파일만 남기고 나머지를 보관 폴더로 옮긴다. 폴더만 보면 "옮겨진 회차"와 "아직 못 받은 회차"를
구분할 수 없어서(파일이 하나뿐이면 그 이후만 받는 규칙 때문에) 중간이 비어 있는 작품은 빠진 회차를 영영 못 받았다. 기록이 있으면
폴더에 없어도 받은 회차로 보고, 정말 못 받은 회차(실패했거나 기다무 대기 중인 것)만 빈 회차로 남는다.

- 기록이 없는 작품(이 기능이 들어오기 전부터 받던 작품): 시작할 때 한 번(backfill_all), 또는 첫 자동 다운로드 때(sync_before_planning)
  폴더의 파일 번호 + 아카이빙 이력(옮긴 파일 이름)으로 처음 기록을 만든다. 폴더 안의 빈 회차는 기록하지 않아서 받게 된다.
- 그 뒤로는 회차를 저장하는 곳(kakao_page_download.download_episode — 자동/수동/기다무 모두 거친다)에서 받을 때마다 더한다.
- 폴더를 비우거나 없애도(전체를 아카이빙한 경우 등) 기록은 지우지 않는다 — 기록에 없는 회차만 받는다. 기록은 날짜로도 지워지지 않는다(아카이빙 이력과 달리).
  처음부터 다시 받으려면 수동 다운로드에서 "받지 않은 것으로 기록"으로 직접 지정한다. 아카이빙으로 옮기기 직전에는 폴더의 회차를 기록에 확정해 둔다(remember_folder_episodes).
- 표식 파일 하나만 있고 아카이빙 이력도 없으면(예전 도구가 남긴 폴더) 파일 번호가 사이트와 다를 수 있어서, 첫 다운로드 때 사이트와 맞춰 본 위치로 기록한다.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import repository
from app.db import write_transaction

log = logging.getLogger(__name__)
PRODUCT_RECEIPTS_PREFIX = "kakao_product_receipts:"


def _decode_products(raw: str | None) -> dict[int, set[int]]:
    if raw is None:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or any(
        not key.isascii() or not key.isdecimal() or int(key) <= 0
        or not isinstance(numbers, list) or not numbers
        or any(type(number) is not int or number <= 0 for number in numbers)
        for key, numbers in data.items()
    ):
        raise ValueError("카카오페이지 상품별 받은 회차 기록이 올바르지 않습니다.")
    return {int(key): set(numbers) for key, numbers in data.items()}


def product_receipts(series_id: int) -> dict[int, set[int]]:
    return _decode_products(repository.get_setting(f"{PRODUCT_RECEIPTS_PREFIX}{series_id}"))


def _save_products(conn, key: str, products: dict[int, set[int]]) -> None:
    encoded = json.dumps({pid: sorted(numbers) for pid, numbers in products.items()})
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))


def discard_removed_receipts(conn, series_id: int, numbers: set[int]) -> None:
    if not numbers:
        return
    key = f"{PRODUCT_RECEIPTS_PREFIX}{series_id}"
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return
    products = _decode_products(row["value"])
    kept = {pid: marks - numbers for pid, marks in products.items() if marks - numbers}
    if kept != products:
        _save_products(conn, key, kept)


def record_download(series_id: int, number: int, product_id: int, *, clear_pending: bool = True) -> None:
    """Preserve the published ZIP's identity after archiving."""
    key = f"{PRODUCT_RECEIPTS_PREFIX}{series_id}"
    with write_transaction() as conn:
        track = conn.execute("SELECT downloaded_numbers, pending_numbers FROM kakao_webtoons WHERE title_id=?", (series_id,)).fetchone()
        if track is None:
            return
        stored = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        products = _decode_products(stored["value"] if stored is not None else None)
        products.setdefault(product_id, set()).add(number)
        _save_products(conn, key, products)
        # A NULL legacy receipt still needs the original file/archive backfill.
        recorded = track["downloaded_numbers"]
        if recorded is not None:
            recorded = json.dumps(sorted(set(json.loads(recorded)) | {number}))
        pending = set(json.loads(track["pending_numbers"] or "[]"))
        if clear_pending:
            pending.discard(number)
        conn.execute("UPDATE kakao_webtoons SET downloaded_numbers=?, pending_numbers=? WHERE title_id=?",
                     (recorded, json.dumps(sorted(pending)) if pending else None, series_id))


def plan_for_catalog(series_id: int, episodes, existing, title: str, *, recorded: set[int] | None = None,
                     pending: set[int] | None = None):
    """Give automatic, manual and preview paths the same read-only ownership decisions."""
    from app import subscription_scope
    from app.kakao_page_download.models import plan_by_folder_rules

    scope = subscription_scope.get("kakao", series_id)
    initial = scope.get("initial_products", {})
    number_by_product = {episode.product_id: episode.number for episode in episodes}
    stale = set()
    for old_number, product_id in initial.items():
        current_number = number_by_product.get(product_id)
        if current_number != int(old_number):
            stale.add(int(old_number))
            if current_number is not None:
                stale.add(current_number)
    if recorded is None:
        recorded = repository.get_kakao_downloaded_numbers(series_id)
    if pending is None:
        pending = repository.get_kakao_pending_numbers(series_id)
    return plan_by_folder_rules(episodes, existing, title, recorded, pending,
                                series_id=series_id, product_receipts=product_receipts(series_id), stale_numbers=stale)


def archived_numbers(series_id: int) -> set[int]:
    """아카이빙 이력에 남은, 이 작품에서 보관 폴더로 옮긴 회차 번호들(알아볼 수 없는 파일 이름은 건너뛴다)."""
    from app import kakao_page_download  # 순환 임포트를 피하려고 함수 안에서

    numbers = set()
    for name in repository.list_archive_file_names(f"kakao_{series_id}"):
        parsed = kakao_page_download.parse_existing_zip_name(Path(name).name)
        if parsed is not None:
            numbers.add(parsed.number)
    return numbers


def initial_numbers(existing, archived: set[int], single_marker_number: int | None = None) -> set[int] | None:
    """처음 만들 기록 = 폴더의 파일 번호 + 아카이빙 이력. 표식 파일 하나뿐이고 이력이 없으면, 사이트와 맞춰 본 표식 위치
    (single_marker_number)를 쓰고 그것도 모르면 None(첫 다운로드 때 만든다)."""
    if len(existing) == 1 and not archived:
        return {single_marker_number} if single_marker_number is not None else None
    return {f.number for f in existing} | archived


def sync_before_planning(series_id: int, existing, plan_without_record) -> set[int] | None:
    """다운로드 계획을 세우기 전에 기록을 폴더 상태에 맞춘다. 계획에 쓸 기록(없으면 None)을 돌려준다.
    폴더가 비었으면 기록을 그대로 둔다(없으면 None), 기록이 없으면 처음 만들고, 있으면 폴더에 새로 생긴 파일 번호를 더한다."""
    recorded = repository.get_kakao_downloaded_numbers(series_id)
    if not existing:
        return recorded or None
    folder_numbers = {f.number for f in existing}
    if recorded is None:
        marker = plan_without_record.marker
        resolved = marker.resolved_number if marker is not None and not marker.warning else None
        created = initial_numbers(existing, archived_numbers(series_id), resolved)
        if created is None:
            return None
        repository.set_kakao_downloaded_numbers(series_id, created)
        return repository.get_kakao_downloaded_numbers(series_id)  # 추적하지 않는 작품이면 None
    missing = folder_numbers - recorded
    if missing:
        repository.add_kakao_downloaded_numbers(series_id, missing)
        recorded = recorded | missing
    return recorded or None


def remember_folder_episodes(series_id: int, folder_numbers, *, episode_paths=()) -> None:
    """아카이빙으로 옮기기 직전에 호출한다 — 폴더에 있는(옮길 것과 남길 것 모두) 회차를 받은 회차 기록에 확정해 둔다.
    아카이빙 이력은 보관 기간이 지나면 지워지고 파일도 폴더를 떠나서, 이 기록이 이 작품이 받은 회차를 기억하는 유일하게 오래 남는 곳이다.
    기록이 없는 작품이면 기존 방식(아카이빙 이력)으로 먼저 채운 뒤 더한다. 추적하지 않는 작품이면 아무것도 하지 않는다."""
    numbers = {int(n) for n in folder_numbers}
    if not numbers:
        return
    record = repository.get_kakao_downloaded_numbers(series_id)
    if record is None:
        record = archived_numbers(series_id)
    repository.set_kakao_downloaded_numbers(series_id, record | numbers)
    if episode_paths:
        from app.kakao_page_download.models import parse_existing_zip_name, read_zip_identity

        for path in episode_paths:
            episode = parse_existing_zip_name(path.name)
            identity = read_zip_identity(path)
            if episode is not None and identity is not None and identity[0] == series_id:
                record_download(series_id, episode.number, identity[1], clear_pending=False)


def mark(series_id: int, numbers, downloaded: bool, existing) -> dict:
    """수동 다운로드에서 사용자가 고른 회차를 "받은 것으로"(downloaded=True) 또는 "받지 않은 것으로"(False) 기록한다 — 기존 기록을 사용자가 정한 대로 다시 쓴다.
    - 받은 것으로: 기록에 더하고 "받을 대상 지정"에서 뺀다. 폴더 파일은 건드리지 않는다.
    - 받지 않은 것으로: 기록에서 빼고 받을 대상으로 지정한다(시작 지점보다 앞이어도 자동으로 받는다). 폴더에 파일이 있는 회차는 파일이 있는 한 받은 것이라
      바꿀 수 없어서 ignored_in_folder로 돌려준다.
    기록이 아직 없으면 폴더 파일과 아카이빙 이력으로 만든 기준(처음 기록을 만들 때와 같은 재료)에서 시작한다. 구독 중인 작품인지는 호출부가 확인한다."""
    wanted = {int(n) for n in numbers}
    folder_numbers = {f.number for f in existing}
    record = repository.get_kakao_downloaded_numbers(series_id)
    if record is None:
        record = folder_numbers | archived_numbers(series_id)
    pending = repository.get_kakao_pending_numbers(series_id)
    if downloaded:
        record = record | wanted | folder_numbers      # 폴더에 있는 회차는 어차피 받은 것이라 함께 기록해 폴더와 기록이 어긋나지 않게 한다
        pending = pending - wanted
        marked, ignored = sorted(wanted), []
    else:
        ignored = sorted(wanted & folder_numbers)
        targets = wanted - folder_numbers
        record = record - targets
        pending = pending | targets
        marked = sorted(targets)
    repository.set_kakao_downloaded_numbers(series_id, record)
    repository.set_kakao_pending_numbers(series_id, pending)
    return {"marked": marked, "ignored_in_folder": ignored, "recorded_count": len(record), "pending_count": len(pending)}


def backfill_all(settings) -> list[str]:
    """시작할 때 한 번 — 기록이 없는 모든 카카오 작품에 대해 폴더와 아카이빙 이력으로 처음 기록을 만든다(폴더 규칙에 쓰는 사이트 연결 없이).
    결과를 작품마다 한 줄로 돌려준다(실행 기록에 남겨서 확인할 수 있게). 폴더가 없거나 비어 있으면 건너뛰고, 이미 기록이 있으면 건드리지 않는다."""
    from app import download_roots, kakao_page_download

    root = download_roots.kakao_root(settings)
    lines = []
    for webtoon in repository.list_kakao_webtoons_without_record():
        title = webtoon["title"]
        try:
            existing = kakao_page_download.scan_existing_files(kakao_page_download.series_folder(root, title))
            if not existing:
                continue
            archived = archived_numbers(webtoon["title_id"])
            created = initial_numbers(existing, archived)
            if created is None:
                lines.append(f"[{title}] 표식 파일 1개뿐이라 받은 회차 기록은 첫 다운로드 때 사이트와 맞춰 본 뒤 만듭니다")
                continue
            repository.set_kakao_downloaded_numbers(webtoon["title_id"], created)
            folder_numbers = {f.number for f in existing}
            holes = [n for n in range(min(folder_numbers), max(folder_numbers) + 1) if n not in folder_numbers and n not in archived]
            detail = f"폴더 {len(folder_numbers)}개 + 아카이빙 이력 {len(archived - folder_numbers)}개"
            lines.append(f"[{title}] 받은 회차 기록 {len(created)}개 생성({detail}" + (f", 폴더 안 빈 회차 {len(holes)}개는 다음 다운로드에서 받음)" if holes else ")"))
        except Exception as e:
            log.warning("받은 회차 기록 만들기 실패(건너뜀) %s: %s", title, e)
            lines.append(f"[{title}] 받은 회차 기록을 만들지 못했습니다: {e}")
    return lines


def backfill_and_log(settings) -> list[str]:
    """backfill_all을 실행하고, 만든 기록이 있으면 요약을 "실행 이력"(다운로드)에 한 건으로 남긴다(설정 > 이력에서 작품마다 몇 개를 기록했는지 확인).
    진행 중인 다운로드 잡의 실시간 로그는 건드리지 않으려고 job_status를 거치지 않고 이력에 바로 쓴다."""
    lines = backfill_all(settings)
    if lines:
        now = datetime.now(timezone.utc).isoformat()
        repository.add_job_history("download", now, now, "success", [f"{now} — 받은 회차 기록 만들기(카카오페이지): {len(lines)}개 작품"] + [f"{now} — {line}" for line in lines])
    return lines
