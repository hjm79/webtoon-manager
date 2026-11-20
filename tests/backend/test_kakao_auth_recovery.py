"""Credential replacement races, real rejection shape, durable pause/resume and failed notices."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from threading import Barrier
from unittest.mock import patch

from aiohttp import web
from fastapi import HTTPException

from app import db, job_status, kakao_page_auth as auth, kakao_page_download as kp, repository, subscription_scope
from app.api import kakao_download as api
from app.config import get_settings
from app.scheduler import download_job as dj
from app.scheduler import kakao_download as scheduled

db.get_connection()


def exported(prefix='TEST', expiry=None):
    return [{'name': name, 'value': f'{prefix}_{index}', 'domain': '.kakao.com', 'path': '/', 'secure': True,
             'expirationDate': time.time()+86400*20 if expiry is None else expiry} for index,name in enumerate(auth.REQUIRED_COOKIES)]


class Response:
    def __init__(self, data=None, status=200, cookie=None):
        self.status, self.data = status, data
        self.cookies = SimpleCookie()
        if cookie:
            self.cookies.load(cookie)
    async def json(self, **kw): return self.data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class Session:
    def __init__(self, handler): self.handler=handler
    def post(self,url,**kw): return self.handler(str(url))
    def get(self,url,**kw): return self.handler(str(url))
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False


GOOD={'result_code':0,'profile':{'uid':1}}
REJECT={'result_code':-100,'message_key':'api_user_authorize_not_found','message':'권한 인증 실패'}


def invalid_imports():
    auth.save_cookies(exported('ORIGINAL'),verified=True)
    before=(repository.get_setting(auth.SETTING_KEY), auth.version())
    bad=[]
    for value in ('bad',float('inf'),True,-1):
        items=exported();items[0]['expirationDate']=value;bad.append(items)
    items=exported();items[0]['value']='';bad.append(items)
    items=exported();items[0]['value']='x\r\ny';bad.append(items)
    items=exported()
    for item in items:item['domain']='notkakao.example'
    bad.append(items)
    for items in bad:
        try:auth.parse_cookie_export(json.dumps(items))
        except ValueError:pass
        else:raise AssertionError('Invalid import accepted')
    assert (repository.get_setting(auth.SETTING_KEY),auth.version())==before
    print('Invalid imports preserve original credentials')


async def cookie_rotation():
    items=exported();items[0]['expirationDate']=time.time()-1
    auth.save_cookies(items)
    client=kp.client_from_saved_cookies(Session(lambda _:Response(GOOD)),1)
    client._absorb_cookies(Response(cookie='_kau=REFRESHED; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure'))
    kp.persist_refreshed_cookies(client)
    assert auth.cookie_status(auth.load_cookies())['days_left']>=19
    client._absorb_cookies(Response(cookie='_kau=; Domain=.kakao.com; Path=/; Max-Age=0'))
    kp.persist_refreshed_cookies(client)
    assert '_kau' not in auth.cookie_map(auth.load_cookies())
    assert '_kau=' not in client._headers(url='https://bff-page.kakao.com/')['Cookie']
    auth.save_cookies(exported('OLD'))
    old=kp.client_from_saved_cookies(None,1)
    auth.save_cookies(exported('NEW'),verified=True)
    before=repository.get_setting(auth.SETTING_KEY)
    old._absorb_cookies(Response(cookie='_kau=LATE; Domain=.kakao.com; Path=/'))
    try:kp.persist_refreshed_cookies(old)
    except auth.KakaoAuthPending as exc:assert exc.state=='credentials_changed'
    else:raise AssertionError('Stale worker overwrote replacement')
    assert repository.get_setting(auth.SETTING_KEY)==before
    assert auth.status()['state']=='healthy'
    entered, release = asyncio.Event(), asyncio.Event()
    class DelayedRejection(Response):
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self
    old=kp.client_from_saved_cookies(Session(lambda _:DelayedRejection(REJECT)),1)
    pending=asyncio.create_task(old.check_login())
    await entered.wait()
    auth.save_cookies(exported('LATEST'),verified=True)
    release.set()
    try:await pending
    except auth.KakaoAuthPending as exc:assert exc.state=='credentials_changed'
    else:raise AssertionError('Delayed old rejection replaced the new authentication state')
    assert auth.status()['state']=='healthy'
    print('Expiry extension, deletion and old-worker fencing verified')


async def response_classification():
    for data,status,expected in ((REJECT,200,False),(None,401,False),({'result_code':500},200,None),
                                  (None,403,None),(None,429,None),(None,503,None),(GOOD,200,True)):
        client=kp.KakaoPageClient(Session(lambda _,d=data,s=status:Response(d,s)),{},1)
        assert await client.check_login() is expected
    auth.save_cookies(exported(),verified=True)
    client=kp.client_from_saved_cookies(Session(lambda _:Response(REJECT)),1)
    try:await client.list_episodes(990091)
    except auth.KakaoAuthPending:pass
    else:raise AssertionError('Rejected list treated as empty success')
    assert auth.status()['state']=='reauth_required'
    print('Actual Kakao -100 rejection and temporary failures classified separately')


async def candidate_validation():
    auth.save_cookies(exported('BEFORE'),verified=True)
    before=(repository.get_setting(auth.SETTING_KEY),auth.version())
    for data,status,expected in ((REJECT,200,401),(None,503,503)):
        with patch.object(kp,'new_session',lambda:Session(lambda _:Response(data,status))):
            try:await api.set_kakao_page_login(api.KakaoPageLoginIn(cookies_json=json.dumps(exported('BAD'))))
            except HTTPException as exc:assert exc.status_code==expected
            else:raise AssertionError('Unverified candidate promoted')
        assert (repository.get_setting(auth.SETTING_KEY),auth.version())==before
    async def never_download(*a,**kw):raise AssertionError('Unknown login started a download')
    unknown=kp.client_from_saved_cookies(Session(lambda _:Response(None,503)),1)
    with patch.object(kp,'new_session',lambda:Session(lambda _:Response())), \
         patch.object(kp,'client_from_saved_cookies',return_value=unknown),patch.object(kp,'download_selected',never_download):
        await api._run_kakao_manual_download(990091,[1],False)
    assert job_status.snapshot()['manual']['status']=='error'
    print('Failed candidate and unknown manual login do not replace/run')


async def queue_recovery():
    auth.save_cookies(exported('EXPIRES'),verified=True)
    repository.set_setting('kakao_webtoons_enabled','1')
    rows=[{'episode_no':n,'subtitle':str(n),'available':True,'owned':False} for n in range(1,5)]
    choice=subscription_scope.make_choice('kakao','990091','selected',rows,episode_nos=[2,4])
    repository.activate_scoped_subscription('kakao','990091','인증 복구 검증','','',choice,enqueue=True)
    scope_before=repository.get_setting('subscription_scope:kakao:990091')
    profile_calls = 0

    def expired_mid_batch(url):
        nonlocal profile_calls
        if 'get_profile' in url:
            profile_calls += 1
            return Response(GOOD if profile_calls == 1 else REJECT)
        return Response(REJECT)

    client=kp.client_from_saved_cookies(Session(expired_mid_batch),1)
    with patch.object(kp,'client_from_saved_cookies',return_value=client), patch.object(dj, 'start_queued_downloads'):
        await asyncio.wait_for(dj._run_queued_downloads(), timeout=5)
    queue=repository.list_download_queue()
    assert len(queue)==1 and queue[0]['state']=='blocked_auth'
    assert job_status.snapshot()['download']['status']=='error'
    repository.update_download_queue(recover=True)
    assert repository.list_download_queue()[0]['state']=='blocked_auth'
    # Naver can continue behind the blocked Kakao entry.
    repository.upsert_new('990092','뒤에 있는 네이버')
    repository.enqueue_download('naver','990092','뒤에 있는 네이버')
    naver=[]
    async def naver_download(*args):naver.append(True)
    with patch.object(dj,'_download_new_episodes_for_one',naver_download):
        await dj._run_queued_downloads()
    assert naver and len(repository.list_download_queue())==1
    # The real save endpoint validates first and then releases preserved queue entries.
    with patch.object(kp,'new_session',lambda:Session(lambda _:Response(GOOD))),patch.object(dj,'start_queued_downloads') as start:
        result=await api.set_kakao_page_login(api.KakaoPageLoginIn(cookies_json=json.dumps(exported('RENEWED'))))
        assert result['state']=='healthy' and start.called
    assert repository.list_download_queue()[0]['state']=='queued'
    assert repository.get_setting('subscription_scope:kakao:990091')==scope_before
    resumed=[]
    async def kakao_download(settings, failures, **kw):
        resumed.extend(subscription_scope.allowed_numbers('kakao',990091,[1,2,3,4]))
        return []
    with patch.object(dj,'_download_kakao_subscriptions',kakao_download):
        await dj._run_queued_downloads()
    assert resumed==[2,4] and repository.list_download_queue()==[]
    print('Mid-batch expiry preserves queue across restart, skips to Naver and resumes original selected scope')


async def temporary_queue_failure():
    auth.save_cookies(exported('TEMPORARY'),verified=True)
    repository.enqueue_download('kakao','990091','인증 복구 검증')
    client=kp.client_from_saved_cookies(Session(lambda _:Response(None,503)),1)
    with patch.object(kp,'client_from_saved_cookies',return_value=client):
        worker=asyncio.create_task(dj._run_queued_downloads())
        for _ in range(100):
            entries=repository.list_download_queue()
            if entries and entries[0]['state']=='retry_wait':break
            await asyncio.sleep(0.01)
        else:raise AssertionError('Temporary failure did not preserve queue')
        assert entries[0]['retry_at']>time.time() and auth.status()['state']=='unknown'
        assert not dj.download_busy()
        worker.cancel()
        try:await worker
        except asyncio.CancelledError:pass
    repository.update_download_queue(recover=True)
    assert repository.list_download_queue()[0]['state']=='retry_wait'
    repository.cancel_queued_download('kakao','990091')
    print('Temporary failure preserves a bounded retry across restart')


async def notification_failure():
    repository.set_setting(auth._ALERT_EXPIRED_KEY,None)
    async def failure(*args):return False
    with patch.object(auth.discord_config,'get_webhook_url',return_value='https://discord.invalid/test'), \
         patch.object(auth.discord_notify,'send_webhook_notification',failure):
        assert await auth.notify_if_needed(None,False,1) is False
    assert repository.get_setting(auth._ALERT_EXPIRED_KEY) is None
    print('Failed notification does not suppress the next attempt')


async def late_health_and_pause_races():
    for strict in (False, True):
        generation = auth.save_cookies(exported('OLD'), verified=True)
        client = kp.client_from_saved_cookies(Session(lambda _: Response(REJECT)), 1)
        record = auth.record_health

        def replace_before_health(*args, **kwargs):
            auth.save_cookies(exported('NEW'), verified=True)
            return record(*args, **kwargs)

        with patch.object(auth, 'record_health', replace_before_health):
            try:
                if strict:
                    await client._request_json('GET', 'https://bff-page.kakao.com/test', label='race')
                else:
                    await client.check_login()
            except auth.KakaoAuthPending as failure:
                assert failure.state == 'credentials_changed'
            else:
                raise AssertionError('Late health CAS failure was ignored')
        assert auth.status()['state'] == 'healthy'
        repository.enqueue_download('kakao', '990091', '인증 복구 검증')
        item = repository.list_download_queue()[0]
        state, _ = auth.defer_download(item['id'], auth.KakaoAuthPending(credential_version=generation))
        assert state == 'retry_wait' and auth.status()['state'] == 'healthy'
        repository.cancel_queued_download('kakao', '990091')
    # Rejection committed first; replacement wins before the old worker pauses its queue item.
    generation = auth.save_cookies(exported('REJECTED'))
    auth.record_health(False, generation)
    auth.save_cookies(exported('RECOVERED'), verified=True)
    repository.enqueue_download('kakao', '990091', '인증 복구 검증')
    item = repository.list_download_queue()[0]
    assert auth.defer_download(item['id'], auth.KakaoAuthPending(credential_version=generation))[0] == 'retry_wait'
    repository.cancel_queued_download('kakao', '990091')
    print('Late health writes and late queue pauses cannot reject replacement credentials')


async def atomic_promotion_and_restart():
    auth.save_cookies(exported('BEFORE'))
    repository.enqueue_download('kakao', '990091', '인증 복구 검증')
    item = repository.list_download_queue()[0]
    repository.update_download_queue(item['id'], state='blocked_auth')
    before = auth.load_state(), repository.get_setting(auth.SETTING_KEY)
    resume = repository.resume_kakao_auth_queue

    def crash_after_queue_update(**kwargs):
        resume(**kwargs)
        raise RuntimeError('simulated process failure before commit')

    with patch.object(repository, 'resume_kakao_auth_queue', crash_after_queue_update):
        try:
            auth.save_cookies(exported('ROLLBACK'), verified=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Fault injection did not run')
    assert (auth.load_state(), repository.get_setting(auth.SETTING_KEY)) == before
    assert repository.list_download_queue()[0]['state'] == 'blocked_auth'
    auth.save_cookies(exported('COMMITTED'), verified=True)
    assert repository.list_download_queue()[0]['state'] == 'queued'
    # Recover a legacy half-committed healthy/blocked state through the actual startup entry point.
    repository.update_download_queue(item['id'], state='blocked_auth')
    observed = []

    async def observe_worker():
        observed.append(repository.list_download_queue()[0]['state'])

    with patch.object(dj, '_run_queued_downloads', observe_worker):
        dj.start_queued_downloads(recover=True)
        await dj._queue_worker_task
    assert observed == ['queued']
    repository.cancel_queued_download('kakao', '990091')
    print('Credential promotion and queue release roll back together; startup reconciles legacy blocked state')


async def concurrent_cookie_updates():
    items = exported('CONCURRENT')
    items += [{'name': name, 'value': '', 'domain': '.kakao.com', 'path': '/'}
              for name in ('_T_ANO', 'optional_empty')]
    validated = auth.parse_cookie_export(json.dumps(items))
    generation = auth.save_cookies(validated, verified=True)
    clients = [kp.client_from_saved_cookies(Session(lambda _: Response(GOOD)), 1) for _ in range(2)]
    clients[0]._absorb_cookies(Response(cookie='_T_ANO=tracking; Domain=.kakao.com; Path=/; Secure'))
    clients[1]._absorb_cookies(Response(cookie='_kau=CONCURRENT_0; Domain=.kakao.com; Path=/; Max-Age=2592000; Secure'))
    gate = Barrier(2)

    def persist(client):
        gate.wait(timeout=5)
        kp.persist_refreshed_cookies(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(persist, clients))
    saved, current_generation, revision = auth.load_state()
    assert current_generation == generation and revision >= 2
    assert auth.cookie_map(saved)['_T_ANO'] == 'tracking'
    assert next(c for c in saved if c['name'] == '_kau')['expirationDate'] > time.time()+25*86400
    for client in clients:
        assert await client.check_login() is True
        assert client.cookies['optional_empty'] == ''
    # A stale response must not restore an old authentication token.
    stale = kp.client_from_saved_cookies(None, 1)
    current = kp.client_from_saved_cookies(None, 1)
    current._absorb_cookies(Response(cookie='_kau=rotated; Domain=.kakao.com; Path=/; Secure'))
    kp.persist_refreshed_cookies(current)
    stale._absorb_cookies(Response(cookie='_T_ANO=stale; Domain=.kakao.com; Path=/; Secure'))
    try:
        kp.persist_refreshed_cookies(stale)
    except auth.KakaoAuthPending:
        pass
    else:
        raise AssertionError('Old token response overwrote a newer token')
    assert auth.cookie_map(auth.load_cookies())['_kau'] == 'rotated'
    print('Concurrent tracking/expiry updates merge without client interruption; empty optional cookies are accepted')


async def notification_retry_over_http():
    auth.save_cookies(exported('NOTICE'), verified=True)
    attempts, profile_requests = [], []

    async def webhook(request):
        attempts.append(await request.json())
        return web.Response(status=500 if len(attempts) == 1 else 204)

    app = web.Application()
    app.router.add_post('/notice', webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    def profile(url):
        if 'get_profile' in url:
            profile_requests.append(True)
        return Response(REJECT)

    client = kp.client_from_saved_cookies(Session(profile), 1)
    try:
        with patch.object(auth.discord_config, 'get_webhook_url', return_value=f'http://127.0.0.1:{port}/notice'), \
             patch('app.discord_notify.valid_webhook_url', return_value=True), \
             patch.object(kp, 'client_from_saved_cookies', return_value=client):
            for expected_attempts in (1, 2, 2):
                try:
                    await scheduled._download_kakao_subscriptions(get_settings(), [], only=[{'title_id': '990091', 'title': '인증 복구 검증'}])
                except auth.KakaoAuthPending as failure:
                    assert failure.state == 'reauth_required'
                else:
                    raise AssertionError('Rejected profile started a download')
                assert len(attempts) == expected_attempts
                if expected_attempts == 1:
                    assert repository.get_setting(auth._ALERT_EXPIRED_KEY) is None
        assert len(profile_requests) == 2
        assert repository.get_setting(auth._ALERT_EXPIRED_KEY)
    finally:
        await runner.cleanup()
    print('Real HTTP notification failure retries while rejected refresh backoff prevents repeated profile requests')


async def main():
    invalid_imports()
    await cookie_rotation()
    await response_classification()
    await candidate_validation()
    await queue_recovery()
    await temporary_queue_failure()
    await notification_failure()
    await late_health_and_pause_races()
    await atomic_promotion_and_restart()
    await concurrent_cookie_updates()
    await notification_retry_over_http()

asyncio.run(main())
print('All Kakao authentication recovery checks passed')
