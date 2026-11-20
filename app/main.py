"""
앱 진입점. `uvicorn app.main:app`으로 실행한다.

보안: 디스코드 봇 토큰/웹훅 URL/쿠키 값이 로그에 원문으로 찍히지 않도록
RedactingFilter를 전역 로깅에 건다 (요구사항: 민감 정보 마스킹).
토큰이 이제 DB에서도 바뀔 수 있어서(설정 페이지), 시작 시점 값을 고정해두지 않고
매 로그 라인마다 discord_config에서 현재 값을 다시 조회해서 마스킹한다.
"""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import Scope

from app.api.routes import router as api_router
from app.api import viewer_proxy
from app.discord_bot import start_bot, stop_bot
from app.log_redaction import RedactingFilter
from app import download_roots, kakao_catalog, kakao_records, kakao_page_auth, report_dispatch, repository
from app.config import get_settings, warn_removed_env_variables
from app.scheduler import create_scheduler
from app.scheduler import download_job
from app.tracker import ensure_default_tags_seeded


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for logger in (logging.getLogger(), logging.getLogger("uvicorn")):
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    warn_removed_env_variables(logging.getLogger(__name__))  # 예전 환경변수가 남아 있으면 조용히 무시되지 않게 알린다
    ensure_default_tags_seeded()
    await start_bot()
    # 받은 회차 기록이 없는 카카오 작품은 폴더와 아카이빙 이력으로 한 번 만들어 둔다 — 스케줄이 돌기 전에(아카이빙이 먼저 폴더를 비우기 전에) 끝낸다.
    # 실패해도 시작을 막지 않는다(첫 자동 다운로드 때 같은 방식으로 만든다).
    try:
        settings = get_settings()
        mount_problem = await asyncio.to_thread(download_roots.download_root_problem, "kakao", settings)
        if mount_problem:
            logging.getLogger(__name__).warning("카카오 받은 회차 대조 보류 · %s", mount_problem)
        else:
            await asyncio.to_thread(kakao_records.backfill_and_log, settings)
    except Exception:
        logging.getLogger(__name__).exception("받은 회차 기록 만들기 실패(건너뜀)")
    # 다운로드 리포트는 이제 발송 시각 스케줄이 아니라 다운로드가 끝날 때 보낸다 — 작업이 끝나면 report_dispatch가 알도록 연결하고, 옛 발송 시각 설정은 지운다.
    report_dispatch.start()
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        download_job.start_queued_downloads(recover=True)
        # 시작할 때 목록을 갱신한다. 저장된 캐시를 먼저 제공하고, 새 목록을 받는 동안 화면은 기다리지 않는다.
        if repository.get_setting("kakao_webtoons_enabled") == "1":
            kakao_catalog.start_refresh()
        yield
    finally:
        scheduler.shutdown(wait=False)
        try:
            await download_job.stop_queued_downloads()
        finally:
            await stop_bot()


app = FastAPI(title="웹툰 구독 관리", lifespan=lifespan)


@app.exception_handler(kakao_page_auth.KakaoAuthPending)
async def kakao_auth_pending_response(request, exc):
    status = 503 if exc.state == 'unknown' else 409 if exc.state == 'credentials_changed' else 401
    return JSONResponse(status_code=status, content={'detail': str(exc), 'auth_state': exc.state},
                        headers={'Cache-Control': 'no-store'})
app.include_router(api_router)
viewer_proxy.install(app)


class VersionedStaticFiles(StaticFiles):
    def __init__(self, directory: Path):
        super().__init__(directory=directory, html=True)
        assets = (directory / "style.css", *sorted((directory / "js").glob("*.js")))
        digest = hashlib.sha256()
        self.asset_paths = {asset.relative_to(directory).as_posix() for asset in assets}
        for path in sorted(self.asset_paths):
            digest.update(path.encode() + b"\0" + hashlib.sha256((directory / path).read_bytes()).digest())
        self.revision_path = f"ui/{digest.hexdigest()[:20]}/"

        # The document and its scripts must use the same build even when a browser
        # or a proxy still has cached responses for the previous asset URLs.
        index = (directory / "index.html").read_text(encoding="utf-8")
        for path in self.asset_paths:
            index = index.replace(f'="/{path}"', f'="/{self.revision_path}{path}"')
        self.index = index

    async def get_response(self, path: str, scope: Scope) -> Response:
        if path in (".", "index.html") and scope["method"] in ("GET", "HEAD"):
            return HTMLResponse(self.index, headers={"Cache-Control": "no-store"})
        if path.startswith("ui/"):
            if not path.startswith(self.revision_path) or path[len(self.revision_path):] not in self.asset_paths:
                raise HTTPException(status_code=404)
            path = path[len(self.revision_path):]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", VersionedStaticFiles(directory=Path(__file__).parent / "static"), name="static")
