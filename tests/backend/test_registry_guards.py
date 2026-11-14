"""Interest baselines, scoped admission, in-flight opt-out, and metadata-only resync."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx

from app import auto_subscribe as a, db, repository as r, tracker as t, subscription_scope as scopes
from app.config import get_settings
from app.models import TitleInfo

db.get_connection()
import app.main as main
S = get_settings()
EP = [{'episode_no': n, 'available': True, 'owned': False} for n in range(1, 6)]

def candidate(n):
    return {'titleId': str(n), 'titleName': '작품 '+str(n), 'finished': False, 'author': {'writers': [{'id':'writer'}]}}

def choice(mode='future', latest_count=None):
    return scopes.make_choice('naver', '1', mode, EP, latest_count=latest_count)

async def fake_scope(platform, tid, settings):
    policy=a.scope_config()
    return policy, scopes.make_choice(platform, tid, policy['mode'], EP, latest_count=policy['latest_count'])

async def run():
    r.set_watched_author_enabled('writer', True, '작가')
    r.upsert_watched_tag('133', '', False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        assert (await c.post('/api/watched-tags',json={'tag_id':'133','tag_name':'판타지'})).json()['enabled'] is True
        r.set_watched_tag_enabled('133',False)
        r.upsert_watched_tag('133','메타 갱신',True)
        assert not next(x for x in r.list_watched_tags() if x.tag_id=='133').enabled
        assert (await c.post('/api/watched-tags',json={'tag_id':'133','tag_name':'판타지'})).json()['enabled'] is True
        assert (await c.get('/api/settings/auto-subscribe-scope')).json()=={'mode':'future','latest_count':None}
        bad=await c.post('/api/settings/auto-subscribe',json={'enabled':True,'scope':{'mode':'latest','latest_count':0}})
        assert bad.status_code==422 and not a.is_enabled()
        for platform, source, sid in [('naver','artist','writer'),('naver','tag','133'),('kakao','artist','카카오')]:
            r.set_setting('kakao_webtoons_enabled','1')
            r.set_watched_author_enabled('카카오',True,'카카오','kakao')
            assert a.observe(platform,source,sid,sid,[])==[]
            rows=a.observe(platform,source,sid,sid,[{'title_id':'100','title':'새 작품'}])
            assert len(rows)==1 and len(a.observe(platform,source,sid,sid,[{'title_id':'100','title':'새 작품'}]))==1
        rows=(await c.get('/api/registry/discoveries')).json()
        assert {x['platform'] for x in rows}=={'naver','kakao'} and r.get('100') is None
        # Existing subscriptions/exclusions never become candidates or auto-reactivate.
        r.upsert_new('100','보존');r.set_status('100',r.STATUS_EXCLUDED)
        assert all(x['platform']!='naver' for x in a.discoveries())
        for i,(mode,count) in enumerate([('future',None),('latest',2),('all',None)],200):
            await c.post('/api/settings/auto-subscribe',json={'enabled':True,'scope':{'mode':mode,'latest_count':count}})
            policy=a.scope_config();scope=choice(mode,count)
            assert a.add_naver(str(i),'자동','artist',source_ids=['writer'],scope=scope,policy=policy)
            saved=scopes.get('naver',str(i));assert saved['explicit'] and saved['initial_cutoff']==5
            assert scopes.allowed_numbers('naver',str(i),list(range(1,7)))==({'future':{6},'latest':{4,5,6},'all':{1,2,3,4,5,6}}[mode])
        # Policy changed during metadata fetch: old policy cannot be applied.
        policy=a.scope_config();scope=choice('all');a.set_config(True,{'mode':'future'})
        assert not a.add_naver('203','정책 경합','artist',source_ids=['writer'],scope=scope,policy=policy)
        scope=choice();policy=a.scope_config()
        r.set_watched_author_enabled('writer',False,'작가')
        assert not a.add_naver('204','해제 경합','artist',source_ids=['writer'],scope=scope,policy=policy)
        r.set_watched_tag_enabled('133',False)
        assert not a.add_naver('205','태그 경합','tag',source_ids=['133'],scope=scope,policy=policy)
        r.set_watched_author_enabled('카카오',False,'카카오','kakao')
        assert not a.add_kakao(206,'카카오 해제','','',source_id='카카오',scope=scope,policy=policy)
        assert all(r.get(str(n)) is None for n in range(203,206))
        r.set_watched_author_enabled('writer',True,'작가')
        # In-flight provider request/episode request, with a real baseline and real insert guards.
        with patch.object(t.daily_plus,'current_ids',AsyncMock(return_value=set())),patch.object(t,'enrich_one',AsyncMock(return_value=(True,'ok'))):
            r.set_setting('registry_seen:naver:artist:writer','[]')
            with patch.object(t,'_automatic_scope',AsyncMock(side_effect=ValueError('목록 실패'))):
                assert await t._add_discovered_titles(None,S,[candidate(300)],{'writer'})==[]
            assert r.get('300') is None and any(x['title_id']=='300' for x in a.discoveries())
            with patch.object(t,'_automatic_scope',fake_scope):
                assert await t._add_discovered_titles(None,S,[candidate(300)],{'writer'})==[('300','작품 300')]
            async def turn_off(*args):
                r.set_watched_author_enabled('writer',False,'작가');return False
            with patch.object(t,'_is_other_finished',turn_off),patch.object(t,'_automatic_scope',fake_scope):
                assert await t._add_discovered_titles(None,S,[candidate(301)],{'writer'})==[]
            assert r.get('301') is None
        # Resync refreshes metadata without changing existing ON/OFF; unseen authors stay OFF.
        r.set_watched_author_enabled('on',True,'켜짐');r.upsert_watched_author('candidate','후보',False)
        info=TitleInfo('200','자동','',False,'webtoon',False,'',writer_id_name_pairs=[('on','켜짐'),('candidate','후보'),('new','신규')])
        before={x.author_id:x.enabled for x in r.list_watched_authors()}
        with patch.object(t.naver_api,'fetch_title_info',AsyncMock(return_value=info)):
            assert await t.resync_registry(None,S)>0
        after={x.author_id:x.enabled for x in r.list_watched_authors()}
        assert all(after[k]==v for k,v in before.items()) and after['new'] is False
        # Real scope preparation uses metadata only, no download call.
        from app import manual_download
        with patch.object(manual_download,'analyze',AsyncMock(return_value=(info,[NS(episode_no=n,is_locked=False) for n in range(1,6)]))):
            policy,scope=await t._automatic_scope('naver','777',S)
            assert scope['mode']=='future' and scope['initial_cutoff']==5
    print('Tag re-enable, baseline, OFF discovery, all scopes, races, retry, metadata-only resync passed')

asyncio.run(run())
