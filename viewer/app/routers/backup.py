"""Administrator routes for portable viewer backup and restore."""

import asyncio
import json
import logging
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .. import access_control, backup, services

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(access_control.require_admin)])


@router.get("/api/backup")
def export_backup():
    payload = backup.export_snapshot()
    filename = f"webtoon-server-backup-{date.today().isoformat()}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/api/restore")
async def import_backup(body: dict):
    try:
        counts = await asyncio.to_thread(backup.restore_snapshot, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400, "백업에 중복되었거나 저장할 수 없는 행이 있습니다.") from exc

    warnings = []
    if counts["new_profile_links_count"]:
        warnings.append("복원한 새 공유 프로필에는 새 링크가 발급되었습니다. 필요하면 다시 공유해 주세요.")
    if counts["ignored_orphan_rows_count"]:
        warnings.append(f"이미 삭제된 공유 프로필의 기록 {counts['ignored_orphan_rows_count']}개를 제외했습니다.")
    try:
        failed_platforms = await services.scan_restored_library()
        if failed_platforms:
            platforms = ", ".join(failed_platforms)
            warnings.append(f"백업 데이터는 저장되었습니다. 라이브러리 재스캔에 실패한 플랫폼이 있습니다 ({platforms}). 마운트를 확인한 뒤 다시 스캔해 주세요.")
    except Exception:
        log.exception("뷰어 백업 데이터 저장 후 재스캔 실패")
        warnings.append("백업 데이터는 저장되었습니다. 라이브러리 재스캔을 완료하지 못했습니다.")
    return {"ok": True, "warnings": warnings, **counts}
