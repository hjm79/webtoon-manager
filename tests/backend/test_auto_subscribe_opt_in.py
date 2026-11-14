"""Artist/tag subscriptions require explicit opt-in and never reverse user exclusions."""
import asyncio
from unittest.mock import patch

import httpx

from app import auto_subscribe, db, repository, tracker
from app.api import workflow, webtoons
from app.config import get_settings
from app.models import TitleInfo

db.get_connection()
from registry_fakes import automatic_scope
tracker._automatic_scope = automatic_scope
import app.main as main

settings = get_settings()
info = TitleInfo("1", "직접 구독", "", False, "webtoon", False, "",
                 writer_id_name_pairs=[("new", "신규 작가"), ("candidate", "후보 작가"), ("on", "직접 켠 작가"), ("off", "직접 끈 작가")])


async def fetch_info(_session, _semaphore, title_id, _settings):
    return title_id, info, [other("21")]


def other(title_id):
    return {"titleId": title_id, "titleName": "작품 " + title_id, "finished": False,
            "author": {"writers": [{"id": "on"}]}}


async def hidden(*_args, **_kwargs):
    return set()


async def unfinished(*_args):
    return False


async def no_notify(*_args):
    return None


async def enrich(_session, _title_id, _settings, **kwargs):
    assert kwargs["register_authors_enabled"] is False
    return True, "ok"


async def tag_name(*_args):
    return "관심 태그"


async def tag_titles(*_args):
    return [other("2"), other("3"), other("4"), other("31")]


async def run():
    assert repository.get_setting(auto_subscribe.ENABLED_KEY) is None and auto_subscribe.is_enabled() is False
    repository.upsert_new("1", "직접 구독")
    repository.upsert_watched_author("candidate", "후보 작가", False)
    repository.set_watched_author_enabled("on", True, "직접 켠 작가")
    repository.set_watched_author_enabled("off", False, "직접 끈 작가")
    repository.upsert_watched_tag("8", "관심 태그", True)
    repository.upsert_watched_author("카카오 작가", "카카오 작가", True, "kakao")
    repository.set_setting("kakao_webtoons_enabled", "1")
    repository.set_setting("auto_register_author_on_subscribe", "0")
    with patch.object(tracker, "_fetch_info_and_others", fetch_info), \
         patch.object(tracker.daily_plus, "current_ids", hidden), \
         patch.object(tracker, "_is_other_finished", unfinished), \
         patch.object(tracker, "send_webhook_notification", no_notify):
        await tracker.scan_subscriptions_for_updates(None, settings)
        enabled = {author.author_id: author.enabled for author in repository.list_watched_authors()}
        assert enabled == {"new": False, "candidate": False, "on": True, "off": False}, enabled
        assert repository.get("21") is None
        with patch.object(tracker.naver_api, "search_webtoons", side_effect=AssertionError("Opt-out must not discover subscriptions")), \
             patch.object(tracker.naver_api, "fetch_curation_titles", side_effect=AssertionError("Opt-out must not scan auto tags")), \
             patch.object(tracker.kakao_api, "search_by_author", side_effect=AssertionError("Opt-out must not auto-subscribe Kakao")):
            assert await tracker.discover_titles_for_unlinked_watched_authors(None, settings) == []
            await tracker.scan_curation_tags(None, settings)
            assert await tracker.scan_kakao_authors_for_new_titles(None, settings) == 0
        assert not auto_subscribe.add_naver("22", "기본 꺼짐", repository.SOURCE_ARTIST)
        assert not auto_subscribe.add_kakao(500, "기본 꺼짐", "", "")
        print("Default OFF, disabled author registration and explicit author choices preserved OK")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/api/settings/auto-subscribe")).json() == {"enabled": False}
            for invalid in ["true", 1, None]:
                assert (await client.post("/api/settings/auto-subscribe", json={"enabled": invalid})).status_code == 422
                assert auto_subscribe.is_enabled() is False
            assert (await client.post("/api/settings/auto-subscribe", json={"enabled": True})).json() == {"enabled": True}
            for title_id, state in [("2", repository.STATUS_EXCLUDED), ("3", repository.STATUS_UNSUBSCRIBED), ("4", repository.STATUS_UNREGISTERED)]:
                repository.upsert_new(title_id, "보존 " + title_id, mark_ever_subscribed=False)
                repository.set_status(title_id, state)
            repository.set_setting("registry_seen:naver:artist:on", "[]")
            repository.set_setting("registry_seen:naver:tag:8", "[]")
            with patch.object(tracker, "enrich_one", enrich):
                added = await tracker._add_discovered_titles(None, settings, [other(t) for t in ["2", "3", "4", "21"]], {"on"})
                assert added == [("21", "작품 21")]
                assert repository.get("21").added_source == repository.SOURCE_ARTIST
                with patch.object(tracker.naver_api, "fetch_curation_title_name", tag_name), patch.object(tracker.naver_api, "fetch_curation_titles", tag_titles):
                    await tracker.scan_curation_tags(None, settings)
                assert repository.get("31").added_source == repository.SOURCE_TAG
            assert [repository.get(t).status for t in ["2", "3", "4"]] == [repository.STATUS_EXCLUDED, repository.STATUS_UNSUBSCRIBED, repository.STATUS_UNREGISTERED]
            assert repository.get("2").ever_subscribed is False
            print("Explicit opt-in admits new artist/tag titles and preserves existing exclusions OK")

            async def switch_off(*_args):
                auto_subscribe.set_enabled(False)
                return False
            with patch.object(tracker, "_is_other_finished", switch_off):
                assert await tracker._add_discovered_titles(None, settings, [other("23")], {"on"}) == []
            assert repository.get("23") is None
            auto_subscribe.set_enabled(True)
            async def exclude_before_insert(*_args):
                repository.upsert_new("24", "동시에 제외", mark_ever_subscribed=False)
                repository.set_status("24", repository.STATUS_EXCLUDED)
                return False
            with patch.object(tracker, "_is_other_finished", exclude_before_insert):
                assert await tracker._add_discovered_titles(None, settings, [other("24")], {"on"}) == []
            assert repository.get("24").status == repository.STATUS_EXCLUDED
            print("Opt-out and user exclusion between discovery and admission cannot be overwritten OK")

            repository.add_seen_kakao_title("카카오 작가", 499, "기준선")
            repository.upsert_new_kakao_webtoon(502, "제외 카카오", status=repository.STATUS_EXCLUDED)
            repository.upsert_new_kakao_webtoon(504, "기존 카카오")
            async def kakao_titles(*_args):
                return [{"title_id": number, "title_name": f"카카오 {number}"} for number in [501, 502]]
            async def no_authors(*_args):
                return None
            with patch.object(tracker.kakao_api, "search_by_author", kakao_titles), \
                 patch.object(tracker, "_record_kakao_title_authors", no_authors), \
                 patch.object(tracker, "_notify_kakao_auto_add", no_notify):
                assert await tracker.scan_kakao_authors_for_new_titles(None, settings) == 1
            assert repository.get_kakao_webtoon(502)["status"] == repository.STATUS_EXCLUDED
            assert auto_subscribe.kakao_source(501) == repository.SOURCE_ARTIST and auto_subscribe.kakao_source(504) == "unknown"
            async def kakao_switch_off(*_args):
                auto_subscribe.set_enabled(False)
                return [{"title_id": 503, "title_name": "늦은 카카오 응답"}]
            with patch.object(tracker.kakao_api, "search_by_author", kakao_switch_off):
                assert await tracker.scan_kakao_authors_for_new_titles(None, settings) == 0
            assert repository.get_kakao_webtoon(503) is None
            auto_subscribe.set_enabled(True)
            async def tag_switch_off(*_args):
                auto_subscribe.set_enabled(False)
                return [other("32")]
            with patch.object(tracker.naver_api, "fetch_curation_title_name", tag_name), patch.object(tracker.naver_api, "fetch_curation_titles", tag_switch_off):
                await tracker.scan_curation_tags(None, settings)
            assert repository.get("32") is None
            assert (await client.post("/api/settings/auto-subscribe", json={"enabled": False})).json() == {"enabled": False}
            assert repository.get_setting(auto_subscribe.ENABLED_KEY) == "0"
            print("Kakao automatic admission and provenance respect opt-in and explicit exclusion OK")

            async def preview(payload):
                return {"title": "직접 추가", "episodes": [{"episode_no": 1, "available": True, "owned": False}]}
            with patch.object(workflow, "preview", preview), patch.object(webtoons, "_trigger_enrich", lambda *_args, **_kwargs: None):
                response = await client.post("/api/workflow/subscribe", json={"platform": "naver", "title_id": "77", "title": "직접 추가", "scope": {"mode": "future"}, "start_now": False})
                assert response.status_code == 200, response.text
            assert repository.get("77").status == repository.STATUS_ACTIVE and repository.get("77").added_source == repository.SOURCE_MANUAL
            rows = (await client.get("/api/workflow/status")).json()["subscriptions"]
            sources = {(row["platform"], row["title_id"]): row["added_source"] for row in rows}
            assert sources[("naver", "77")] == "manual" and sources[("naver", "21")] == "artist" and sources[("naver", "31")] == "tag"
            assert sources[("kakao", "501")] == "artist" and sources[("kakao", "504")] == "unknown"
            print("Manual subscriptions remain available while opt-out; workflow exposes provenance OK")


asyncio.run(run())
