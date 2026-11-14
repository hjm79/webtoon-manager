"""Failed, partial and truncated lookups must never establish discovery baselines."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from app import auto_subscribe as a, db, repository as r, tracker as t, naver_api, kakao_api, subscription_scope
from app.config import get_settings

db.get_connection()
import app.main as main
S = get_settings()

class Response:
    def __init__(self, status, data): self.status, self.data = status, data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.data

class Session:
    fail_page = None
    new = False
    malformed = False
    def get(self, url, **kwargs):
        params = kwargs.get('params') or {}
        if url == naver_api.NAVER_CURATION_LIST_URL:
            page = params['page']
            if page == self.fail_page: return Response(503, {})
            items = [{'titleId': 100 + page, 'titleName': '기존작', 'finished': False}]
            if page == 2 and self.new: items.append({'titleId': 103, 'titleName': '진짜 신작', 'finished': False})
            return Response(200, {} if self.malformed else {'curationViewList': items, 'pageInfo': {'totalPages': 2}})
        if url == naver_api.NAVER_ARTIST_OTHER_TITLES_URL:
            if self.fail_page is not None: return Response(503, {})
            items = [{'titleId': 302, 'titleName': '기존 다른 작품', 'finished': False, 'author': {'writers': [{'id': 'writer'}]}}]
            if self.new: items.append({'titleId': 303, 'titleName': '작가 신작', 'finished': False, 'author': {'writers': [{'id':'writer'}]}})
            return Response(200, {} if self.malformed else items)
        if url == kakao_api.KAKAO_SEARCH_URL:
            page = params['page']
            if page == self.fail_page: return Response(503, {})
            cards = [{'series_id':201+page, 'title':'기존작', 'category_uid':10, 'authors':'작가A', 'on_issue':'Y'}]
            if page == 1 and self.new: cards.append({'series_id':203,'title':'카카오 신작','category_uid':10,'authors':'작가A','on_issue':'Y'})
            return Response(200, {} if self.malformed else {'result':{'is_end':page == 1,'list':cards}})
        raise AssertionError(url)

async def scope(platform, tid, settings):
    policy = a.scope_config()
    return policy, subscription_scope.make_choice(platform, tid, policy['mode'], [{'episode_no':1,'available':True,'owned':False}])

async def raises(kind, coro):
    try: await coro
    except kind: return
    raise AssertionError('Incomplete lookup did not fail closed')

async def run():
    a.set_enabled(True)
    r.upsert_watched_tag('77', '태그', True)
    session = Session()
    with patch.object(t.daily_plus,'current_ids',AsyncMock(return_value=set())), patch.object(naver_api,'fetch_curation_title_name',AsyncMock(return_value='태그')), \
         patch.object(t,'_automatic_scope',scope), patch.object(t,'enrich_one',AsyncMock(return_value=(True,'ok'))), \
         patch.object(t,'send_webhook_notification',AsyncMock()), patch.object(kakao_api.asyncio,'sleep',AsyncMock()):
        # Both first-page and middle-page failures leave the baseline completely absent.
        for page in (1,2):
            session.fail_page = page
            await t.scan_curation_tags(session,S)
            assert r.get_setting('registry_seen:naver:tag:77') is None and r.list_all() == []
        session.fail_page = None
        await t.scan_curation_tags(session,S)
        baseline = r.get_setting('registry_seen:naver:tag:77')
        assert json.loads(baseline) == ['101','102'] and r.list_all() == []
        session.new = True; session.fail_page = 2
        await t.scan_curation_tags(session,S)
        assert r.get_setting('registry_seen:naver:tag:77') == baseline and r.get('103') is None
        session.fail_page = None
        await t.scan_curation_tags(session,S)
        assert r.get('103').status == 'active' and r.get('101') is r.get('102') is None
        # The artist anchor alone must not become a baseline after the other-titles lookup fails.
        r.set_watched_author_enabled('writer',True,'작가')
        anchor = NS(title_id='301',title_name='기존 앵커',author_ids_names=[('writer','작가')])
        session.fail_page = 1; session.new = False
        with patch.object(naver_api,'search_webtoons',AsyncMock(return_value=[anchor])):
            await t.discover_titles_for_unlinked_watched_authors(session,S)
            assert r.get_setting('registry_seen:naver:artist:writer') is None
            session.fail_page = None
            await t.discover_titles_for_unlinked_watched_authors(session,S)
            assert json.loads(r.get_setting('registry_seen:naver:artist:writer')) == ['301','302']
            assert r.get('302') is None
            session.new = True
            await t.discover_titles_for_unlinked_watched_authors(session,S)
            assert r.get('303').status == 'active'
        # Kakao also requires every page before the baseline is saved.
        r.set_setting('kakao_webtoons_enabled','1');r.set_watched_author_enabled('작가A',True,'작가A','kakao')
        session.new = False
        with patch.object(t,'_record_kakao_title_authors',AsyncMock()),patch.object(t,'_notify_kakao_auto_add',AsyncMock()):
            for page in (0,1):
                session.fail_page = page
                assert await t.scan_kakao_authors_for_new_titles(session,S) == 0
                assert r.get_setting('registry_seen:kakao:artist:작가A') is None
                assert not r.get_seen_kakao_title_ids('작가A')
            session.fail_page = None
            assert await t.scan_kakao_authors_for_new_titles(session,S) == 0
            baseline = r.get_setting('registry_seen:kakao:artist:작가A')
            assert json.loads(baseline) == ['201','202']
            session.new = True;session.fail_page = 1
            assert await t.scan_kakao_authors_for_new_titles(session,S) == 0
            assert r.get_setting('registry_seen:kakao:artist:작가A') == baseline and r.get_kakao_webtoon(203) is None
            session.fail_page = None
            assert await t.scan_kakao_authors_for_new_titles(session,S) == 1
            assert r.get_kakao_webtoon(201) is r.get_kakao_webtoon(202) is None
        # A page limit is an incomplete result; malformed responses also fail closed.
        with patch.object(kakao_api,'_MAX_SEARCH_PAGES',1):
            await raises(kakao_api.KakaoLookupError,kakao_api.search_by_author(session,'작가A',5))
        session.malformed = True
        await raises(kakao_api.KakaoLookupError,kakao_api.search_by_author(session,'작가A',5))
        await raises(naver_api.NaverApiError,naver_api.fetch_other_titles_by_artist(session,'301',5))
        await raises(naver_api.NaverApiError,naver_api.fetch_curation_titles(session,77,5,0))
        # A genuine empty, complete lookup remains a valid baseline.
        class Empty:
            def get(self,url,**kwargs):
                return Response(200,{'curationViewList':[],'pageInfo':{'totalPages':0}} if url == naver_api.NAVER_CURATION_LIST_URL else {'result':{'list':[],'is_end':True}})
        assert await naver_api.fetch_curation_titles(Empty(),77,5,0) == []
        assert await kakao_api.search_by_author(Empty(),'작가A',5) == []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
            with patch.object(kakao_api,'search_by_author',AsyncMock(side_effect=kakao_api.KakaoLookupError('unavailable'))):
                response = await c.post('/api/kakao/watched-authors',json={'author_name':'아직 미등록'})
                assert response.status_code == 502
                assert not any(x.author_id=='아직 미등록' for x in r.list_watched_authors('kakao'))
    print('Complete baselines only; failure/recovery, page limit, empty results and registration errors passed')

asyncio.run(run())
