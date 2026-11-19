"""통합 검색은 실제 제공자 HTTP 오류와 검색 결과 없음, 일부 실패를 구분한다."""

import asyncio
from unittest.mock import AsyncMock, patch

from app import db, repository
from app.api import workflow
from app.constants import NAVER_SEARCH_ALL_URL
from fake_naver_http import FakeResponse, FakeSession


class Session(FakeSession):
    def __init__(self, naver_status, kakao_status):
        super().__init__(self.response)
        self.naver_status = naver_status
        self.kakao_status = kakao_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def response(self, url, _params, _attempt):
        if url == NAVER_SEARCH_ALL_URL:
            return FakeResponse(self.naver_status, json_data={"searchWebtoonResult": {"searchViewList": []}})
        if url == workflow.kakao_api.KAKAO_SEARCH_URL:
            return FakeResponse(self.kakao_status, json_data={"result_code": 0, "result": {"list": [], "is_end": True}})
        raise AssertionError(f"Unexpected request: {url}")


async def main():
    db.get_connection()
    repository.upsert_new("9101", "격리 검색 저장 결과")
    with patch.object(workflow.daily_plus, "current_ids", new=AsyncMock(return_value=set())), patch.object(
        workflow.kakao_api.asyncio, "sleep", new=AsyncMock(),
    ), patch.object(workflow.kakao_catalog, "snapshot", return_value=([], None)):
        for naver_status, kakao_status, errors in ((200, 200, []), (503, 200, ["naver"]), (200, 503, ["kakao"]), (503, 503, ["naver", "kakao"])):
            with patch.object(workflow.aiohttp, "ClientSession", return_value=Session(naver_status, kakao_status)):
                empty = await workflow.search(query="검색되지 않는 입력", platform="all")
                assert empty["results"] == []
                assert [item["platform"] for item in empty["errors"]] == errors
                local = await workflow.search(query="격리 검색", platform="all")
                assert [row["title_id"] for row in local["results"]] == ["9101"]
                assert [item["platform"] for item in local["errors"]] == errors


asyncio.run(main())
