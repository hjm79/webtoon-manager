"""카카오페이지 HTTP 클라이언트(쿠키, 재시도, 회차 목록, 이용권, 이미지) — 서버와 주고받는 부분.
"""

from __future__ import annotations

import asyncio
import logging
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import aiohttp
from yarl import URL

from app import kakao_api, kakao_page_auth

from app.kakao_page_download.models import (
    ABOUT_URL,
    OVERVIEW_URL,
    Episode,
    PRODUCT_LIST_URL,
    PROFILE_URL,
    RENTAL_TICKET_TYPE,
    TICKET_MY_URL,
    TICKET_READY_URL,
    TICKET_USE_URL,
    TicketInfo,
    VIEWER_DATA_URL,
    WAITFREE_TICKET_TYPE,
    _IMAGE_RETRIES,
    _KST,
    _LIST_WINDOW_SIZE,
    _MAX_LIST_PAGES,
    _PAGE_INTERVAL_SECONDS,
    _parse_episode,
    is_excluded_item,
)

log = logging.getLogger(__name__)

# ── 클라이언트 ────────────────────────────────────────────────────────────

def _absorb_entries(entries: list[dict], episodes: dict[int, Episode | None], excluded_orders: list[int], series_title: str, now_kst: datetime) -> int:
    """한 페이지의 항목을 episodes(상품 번호 → 회차)에 더하고 새로 센 항목 수를 돌려준다. 상품 번호가 없거나 이미 본 항목은 건너뛴다.
    동영상/체험판 회차는 목록에서 빼되(다시 세지 않게 None으로 표시만) 순서 번호를 excluded_orders에 남긴다 — 뒤 회차 번호를 그만큼 당기는 데 쓴다."""
    new_count = 0
    for entry in entries:
        item = entry.get("item") or {}
        product_id = item.get("product_id")
        if product_id is None or product_id in episodes:
            continue
        if is_excluded_item(item):
            excluded_orders.append(int(item.get("order_value") or 0))
            episodes[product_id] = None
        else:
            episodes[product_id] = _parse_episode(item, series_title, now_kst)
        new_count += 1
    return new_count


def _renumber_after_exclusion(episodes: dict[int, Episode | None], excluded_orders: list[int]) -> list[Episode]:
    """번호가 0 이하인 항목(예고 등)을 빼고, 제외된 동영상/체험판이 앞에 있던 만큼 회차 번호를 당긴 뒤 번호 오름차순으로 돌려준다."""
    images = [e for e in episodes.values() if e is not None and e.number > 0]
    for episode in images:
        episode.number -= sum(1 for order in excluded_orders if order < episode.number)
    return sorted(images, key=lambda e: e.number)


class KakaoPageClient:
    """로그인 쿠키를 붙여 카카오페이지 API/이미지를 부르는 클라이언트. 세션은 쿠키 저장소가 없는
    것(aiohttp.DummyCookieJar)으로 만들어 쓴다 — 쿠키는 이 객체가 직접 관리하고, 서버가 새로 내려준
    쿠키는 cookies에 반영하며 cookies_changed로 표시한다(호출부가 끝나고 저장)."""

    def __init__(self, session: aiohttp.ClientSession, cookies: dict[str, str], timeout_seconds: int, *, cookie_entries=None, credential_version=None, cookie_revision=None):
        self.credential_version = credential_version
        self.cookie_entries = [dict(c) for c in cookie_entries] if cookie_entries is not None else [
            {"name": name, "value": value, "domain": ".kakao.com", "path": "/", "secure": True} for name, value in cookies.items()]
        self.persisted_cookie_entries = [dict(c) for c in self.cookie_entries]
        self.cookie_revision = cookie_revision
        self.last_problem = None
        self._last_url = "https://bff-page.kakao.com/"
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.cookies = dict(cookies)
        self.cookies_changed = False
        self.last_failure: str | None = None  # 가장 최근 받기 실패의 원인(화면 로그 한 줄에 붙인다). 새 회차를 받기 시작할 때 지운다
        self.last_waitfree_charged_at: str | None = None  # 가장 최근 기다무 사용 응답이 알려 준 다음 충전 시각(waitfree_charged_at)

    def _headers(self, *, image: bool = False, post: bool = False, url=None) -> dict:
        headers = dict(kakao_api.IMAGE_HEADERS if image else kakao_api.HEADERS)
        target = URL(url or self._last_url)
        now = time.time()
        matches = []
        for cookie in self.cookie_entries:
            domain = cookie['domain'].lower().lstrip('.')
            host = target.host or ''
            domain_ok = host == domain or (not cookie.get('hostOnly') and host.endswith('.' + domain))
            path = cookie.get('path', '/')
            path_ok = target.path == path or target.path.startswith(path.rstrip('/') + '/')
            expiry = cookie.get('expirationDate')
            if domain_ok and path_ok and (not cookie.get('secure') or target.scheme == 'https') and (expiry is None or expiry > now):
                matches.append(cookie)
        matches.sort(key=lambda c: len(c.get('path', '/')), reverse=True)
        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in matches)
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _ensure_current(self, *, refresh=True):
        if self.credential_version is None:
            return
        if refresh:
            cookies, generation, revision = kakao_page_auth.load_state()
        else:
            generation, revision, cookies = kakao_page_auth.version(), self.cookie_revision, None
        if generation != self.credential_version:
            raise kakao_page_auth.KakaoAuthPending('credentials_changed', 'credential_version_changed', self.credential_version)
        if refresh and not self.cookies_changed and revision != self.cookie_revision:
            self.cookie_entries = [dict(c) for c in cookies or []]
            self.persisted_cookie_entries = [dict(c) for c in self.cookie_entries]
            self.cookies = kakao_page_auth.cookie_map(self.cookie_entries)
            self.cookie_revision = revision

    def _record_health(self, logged_in, expected_cookies):
        accepted = kakao_page_auth.record_health(logged_in, self.credential_version, self.last_problem,
                                                expected_cookies=expected_cookies)
        if self.credential_version is not None and not accepted:
            raise kakao_page_auth.KakaoAuthPending('credentials_changed', 'credential_version_changed', self.credential_version)

    def _absorb_cookies(self, response, *, request_url=None) -> None:
        source = URL(str(getattr(response, 'url', None) or request_url or self._last_url))
        host = source.host or ''
        if host != 'kakao.com' and not host.endswith('.kakao.com'):
            return
        for name, morsel in response.cookies.items():
            domain = (morsel['domain'] or host).lower().lstrip('.')
            if host != domain and not host.endswith('.' + domain):
                continue
            if domain != 'kakao.com' and not domain.endswith('.kakao.com'):
                continue
            path = morsel['path'] or source.path.rpartition('/')[0] or '/'
            expiry = None
            try:
                if morsel['max-age']:
                    expiry = time.time() + int(morsel['max-age'])
                elif morsel['expires']:
                    date = parsedate_to_datetime(morsel['expires'])
                    expiry = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
            except (ValueError, TypeError, OverflowError):
                continue
            identity = (name, domain, path)
            old = [c for c in self.cookie_entries if (c['name'], c['domain'].lstrip('.').lower(), c.get('path', '/')) == identity]
            kept = [c for c in self.cookie_entries if c not in old]
            if (morsel.value or name not in kakao_page_auth.REQUIRED_COOKIES) and (expiry is None or expiry > time.time()):
                entry = {'name': name, 'value': morsel.value, 'domain': morsel['domain'] or host, 'path': path,
                         'expirationDate': expiry, 'secure': bool(morsel['secure']), 'hostOnly': not bool(morsel['domain'])}
                try:
                    kakao_page_auth.validate_cookies([entry], require_all=False)
                except ValueError:
                    continue
                kept.append(entry)
            if kept != self.cookie_entries:
                self.cookie_entries = kept
                self.cookies = kakao_page_auth.cookie_map(kept)
                self.cookies_changed = True

    async def _request_json(self, method: str, url: str, *, params: dict | None = None, label: str,
                            data: dict | None = None, auth_check=False, _retried=False):
        self._ensure_current()
        request_revision = self.cookie_revision
        request_cookies = self.cookie_entries
        self._last_url = url
        status, payload, response, problem = None, None, None, None
        try:
            if method == 'POST':
                request = self._session.post(url, data=data or {}, headers=self._headers(post=True, url=url), timeout=self._timeout, allow_redirects=False)
            else:
                request = self._session.get(url, params=params, headers=self._headers(url=url), timeout=self._timeout, allow_redirects=False)
            async with request as response:
                self._ensure_current(refresh=False)
                status = response.status
                payload = await response.json(content_type=None) if status == 200 else None
        except kakao_page_auth.KakaoAuthPending:
            raise
        except Exception as exc:
            log.warning('카카오페이지 요청 실패 (%s): %s', label, type(exc).__name__)
            problem = 'network_error'
        self._ensure_current(refresh=False)
        code = str(payload.get('result_code')) if isinstance(payload, dict) and 'result_code' in payload else None
        rejected = status == 401 or code in ('401', '-401', 'UNAUTHORIZED', 'LOGIN_REQUIRED') or (
            code == '-100' and isinstance(payload, dict) and payload.get('message_key') == 'api_user_authorize_not_found')
        if rejected:
            problem = 'session_rejected'
        elif status == 429:
            problem = 'rate_limited'
        elif status == 403:
            problem = 'access_blocked'
        elif status is None or status >= 500:
            problem = problem or 'server_error'
        elif status != 200 or not isinstance(payload, dict):
            problem = problem or 'invalid_response'
        elif code is not None and code != '0':
            problem = 'api_error'
        self.last_problem = problem
        logged_in = None
        if auth_check:
            if rejected:
                logged_in = False
            elif problem is None and code == '0' and isinstance(payload.get('profile'), dict):
                logged_in = bool(payload['profile'].get('uid'))
        if problem is None and (not auth_check or logged_in is True) and self.cookie_revision == request_revision:
            # Error responses can clear or replace cookies still usable for refresh.
            self._absorb_cookies(response, request_url=url)
            request_cookies = self.cookie_entries
            persist_refreshed_cookies(self, strict_auth=True)
        if auth_check:
            self._record_health(logged_in, request_cookies)
            return logged_in
        if problem:
            if rejected:
                if _retried and self.credential_version is not None:
                    raise kakao_page_auth.KakaoAuthPending('unknown', 'request_rejected_after_refresh', self.credential_version)
                self._record_health(False, request_cookies)
                if self.credential_version is not None and not _retried:
                    recovered = await self._recover_session()
                    if recovered is True:
                        if method == 'GET':
                            return await self._request_json(method, url, params=params, label=label,
                                                            data=data, _retried=True)
                        # Mutating requests are never replayed; the durable queue rechecks availability.
                        if kakao_page_auth._auth_snapshot(self.cookie_entries) == kakao_page_auth._auth_snapshot(request_cookies):
                            raise kakao_page_auth.KakaoAuthPending('unknown', 'request_rejected_after_refresh', self.credential_version)
                        raise kakao_page_auth.KakaoAuthPending('credentials_changed', 'session_refreshed', self.credential_version)
                    if recovered is None:
                        raise kakao_page_auth.KakaoAuthPending('unknown', 'refresh_pending', self.credential_version)
                raise kakao_page_auth.KakaoAuthPending(credential_version=self.credential_version)
            if self.credential_version is not None and problem != 'api_error':
                self._record_health(None, request_cookies)
            return status, payload if problem == 'api_error' else None
        return status, payload

    async def _recover_session(self) -> bool | None:
        from app.kakao_session_refresh import refresh_saved_session

        result = await refresh_saved_session(session=self._session, force=True)
        if result['state'] == 'credentials_changed':
            raise kakao_page_auth.KakaoAuthPending('credentials_changed', result['reason'], self.credential_version)
        self.cookies_changed = False
        self._ensure_current()
        if result['state'] == 'healthy':
            self.last_problem = None
            return True
        if result['state'] in ('unknown', 'deferred'):
            return None
        return False

    async def check_login(self) -> bool | None:
        logged_in = await self._request_json('POST', PROFILE_URL, label='로그인 확인', auth_check=True)
        if logged_in is False and self.credential_version is not None:
            return await self._recover_session()
        return logged_in

    def raise_if_unavailable(self):
        if self.credential_version is not None and self.last_problem in (
                'network_error', 'server_error', 'rate_limited', 'access_blocked', 'invalid_response'):
            raise kakao_page_auth.KakaoAuthPending('unknown', self.last_problem, self.credential_version)

    async def require_login(self):
        logged_in = await self.check_login()
        persist_refreshed_cookies(self)
        if logged_in is not True:
            raise kakao_page_auth.KakaoAuthPending('reauth_required' if logged_in is False else 'unknown',
                                                 self.last_problem or 'invalid_response', self.credential_version)
        return True

    async def list_episodes(self, series_id: int) -> tuple[dict, list[Episode]] | None:
        """(작품 정보, 회차 목록(번호 오름차순))를 돌려준다. 중간에 하나라도 실패하면 None — 일부만 본 목록으로
        받을 회차를 정하면 안 되기 때문이다."""
        now_kst = datetime.now(_KST).replace(tzinfo=None)
        series_item: dict = {}
        episodes: dict[int, Episode] = {}
        excluded_orders: list[int] = []  # 동영상/체험판 회차의 순서 번호(뒤 회차의 번호를 그만큼 당기는 데 쓴다)
        cursor, direction = 0, "NEXT"
        for _ in range(_MAX_LIST_PAGES):
            params = {
                "series_id": series_id, "cursor_index": cursor, "cursor_direction": direction, "window_size": _LIST_WINDOW_SIZE,
                "sort_type": "asc",  # 계정에 저장된 정렬과 무관하게 항상 첫화부터
            }
            _, data = await self._request_json("GET", PRODUCT_LIST_URL, params=params, label=f"회차 목록 series_id={series_id}")
            if data is None or ('result_code' in data and data['result_code'] != 0):
                return None
            result = data.get("result") or {}
            if not series_item:
                series_item = result.get("series_item") or {}
            entries = result.get("list") or []
            new_count = _absorb_entries(entries, episodes, excluded_orders, series_item.get("title", ""), now_kst)
            cursor = entries[-1].get("cursor_index") if entries else None
            if not result.get("has_next") or new_count == 0 or cursor is None:
                break
            await asyncio.sleep(_PAGE_INTERVAL_SECONDS)
        series_item["_excluded_video_count"] = len(excluded_orders)  # 동영상/체험판이라 목록에서 뺀 회차 수(분석 화면의 "사이트 회차 수" 비교용)
        return series_item, _renumber_after_exclusion(episodes, excluded_orders)

    async def ticket_info(self, series_id: int) -> TicketInfo | None:
        """이 작품의 대여권/소장권 수와 기다무 상태. 못 받으면 None(기다무를 못 쓰는 것으로 다룬다)."""
        _, data = await self._request_json(
            "GET", TICKET_MY_URL, params={"series_id": series_id, "include_waitfree": "true"}, label=f"이용권 series_id={series_id}"
        )
        if not data or data.get("result_code") != 0:
            return None
        result = data.get("result") or {}
        mine, waitfree = result.get("my") or {}, result.get("waitfree") or {}
        ready = bool(waitfree.get("charged_complete"))
        return TicketInfo(
            rental_count=int(mine.get("ticket_rental_count") or 0), own_count=int(mine.get("ticket_own_count") or 0),
            waitfree_ready=ready, waitfree_available_at=None if ready else waitfree.get("charged_at"),
            waitfree_period_minutes=int(waitfree.get("charged_period_by_minute") or 0),
        )

    async def _ready_to_use(self, product_id: int, label: str) -> dict | None:
        """회차 하나의 이용권 사용 가능 정보(ticket/ready_to_use의 result). 못 받았거나 result_code가 0이 아니면 None."""
        _, ready = await self._request_json("GET", TICKET_READY_URL, params={"product_id": product_id, "include_series": "true"}, label=f"{label} product_id={product_id}")
        if not ready or ready.get("result_code") != 0:
            self.raise_if_unavailable()
            return None
        return ready.get("result") or {}

    async def use_waitfree_ticket(self, product_id: int) -> tuple[bool, str]:
        """회차 하나를 기다무 대여권으로 연다. (성공 여부, 사람이 읽을 이유). 이 회차에 실제로 쓸 수 있는 이용권이
        기다무(RT05)라고 서버가 알려줄 때만 쓴다 — 선물권 등 다른 이용권이나 결제가 필요한 경우는 쓰지 않는다."""
        result = await self._ready_to_use(product_id, "이용권 확인")
        if result is None:
            return False, "이용권 상태를 확인하지 못했습니다"
        if (result.get("single") or {}).get("waitfree_block"):
            return False, "기다무로 열 수 없는 회차입니다"
        ticket_type = ((result.get("available") or {}).get("ticket_rental_type"))
        if ticket_type != WAITFREE_TICKET_TYPE:
            return False, "기다무를 쓸 수 없습니다" + (f"(가능한 이용권: {ticket_type})" if ticket_type else "")
        status, used = await self._request_json(
            "POST", TICKET_USE_URL, data={"product_id": product_id, "ticket_type": WAITFREE_TICKET_TYPE}, label=f"기다무 사용 product_id={product_id}"
        )
        if not used or used.get("result_code") != 0:
            return False, f"기다무 사용에 실패했습니다(HTTP {status})"
        self.last_waitfree_charged_at = (used.get("result") or {}).get("waitfree_charged_at")  # 다음 충전 시각 — 이 시각에 맞춰 받기에 쓴다
        return True, "기다무로 열었습니다"

    async def use_rental_ticket(self, product_id: int) -> tuple[bool, str, int | None]:
        """회차 하나를 일반 대여권(RT01)으로 연다. (성공 여부, 사람이 읽을 이유, 이 작품에 남은 대여권 수 또는 None).
        서버가 이 회차에 쓸 수 있는 이용권이 RT01이라고 알려 주고, 보유 대여권이 1장 이상이고, 기다무로 열 수 없는 회차(기다무가 없는 작품이거나
        waitfree_block)일 때만 쓴다 — 기다무로 열 수 있는 회차에 대여권을 쓰는 낭비와 선물권/결제를 막는다. 사용 요청은 **한 번만** 보낸다:
        응답을 못 받았으면 서버가 처리했는지 다시 조회해서(대여 중이 됐는지) 판단하고, 절대 다시 보내지 않는다(이중 소모 방지)."""
        result = await self._ready_to_use(product_id, "대여권 확인")
        if result is None:
            return False, "이용권 상태를 확인하지 못했습니다", None
        series_has_waitfree = (result.get("series") or {}).get("is_waitfree")
        if series_has_waitfree is None:
            return False, "작품의 기다무 정보를 확인하지 못했습니다", None
        if series_has_waitfree and not (result.get("single") or {}).get("waitfree_block"):
            return False, "기다무로 열 수 있는 회차라 대여권을 쓰지 않습니다", None
        count = int((result.get("my") or {}).get("ticket_rental_count") or 0)
        ticket_type = (result.get("available") or {}).get("ticket_rental_type")
        if ticket_type != RENTAL_TICKET_TYPE or count < 1:
            return False, "사용할 수 있는 대여권이 없습니다" + (f"(가능한 이용권: {ticket_type})" if ticket_type and ticket_type != RENTAL_TICKET_TYPE else ""), count
        status, used = await self._request_json(
            "POST", TICKET_USE_URL, data={"product_id": product_id, "ticket_type": RENTAL_TICKET_TYPE}, label=f"대여권 사용 product_id={product_id}"
        )
        if used is not None:
            if used.get("result_code") == 0:
                return True, "대여권으로 열었습니다", count - 1
            return False, f"대여권 사용에 실패했습니다(HTTP {status})", count
        # 응답을 받지 못했다 — 서버가 처리했을 수도 있으니, 다시 보내지 않고 대여 중이 됐는지만 확인한다
        check = await self._ready_to_use(product_id, "대여권 사용 확인")
        if check is not None and (check.get("single") or {}).get("read_access_type") == "RAT2":
            return True, "대여권으로 열었습니다(응답은 못 받았지만 대여 중으로 확인됨)", count - 1
        return False, f"대여권 사용 결과를 확인하지 못했습니다(HTTP {status}) — 다시 시도하지 않습니다", count

    async def fetch_series_status(self, series_id: int) -> tuple[int | None, dict | None]:
        """(HTTP 상태, 작품 기본 정보)를 돌려준다. 정보가 없으면 (상태, None), 요청 자체가 실패하면 (None, None) — 작품이 사라진 것(404)과
        일시 오류(네트워크/5xx)를 구분하는 데 쓴다. 기본 정보(제목/설명/장르/연령/연재 상태/작가)는 회차 목록의 첫 페이지 응답에 들어 있어서
        회차 1개만 요청해 얻는다(전체 회차를 넘기지 않으므로 가볍다)."""
        status, data = await self._request_json(
            "GET", PRODUCT_LIST_URL,
            params={"series_id": series_id, "cursor_index": 0, "cursor_direction": "NEXT", "window_size": 1, "sort_type": "asc"},
            label=f"작품 정보 series_id={series_id}",
        )
        result = (data or {}).get("result") or {}
        return status, result.get("series_item") or None

    async def fetch_series_item(self, series_id: int) -> dict | None:
        """작품 기본 정보만(못 받으면 None) — 상태가 필요하면 fetch_series_status."""
        return (await self.fetch_series_status(series_id))[1]

    async def fetch_about(self, series_id: int) -> dict | None:
        """작품 "정보" 탭: 글/그림/원작 작가(role), 테마 키워드 등. 못 받으면 None."""
        _, data = await self._request_json("GET", ABOUT_URL, params={"series_id": series_id}, label=f"작품 정보 series_id={series_id}")
        if not data or data.get("result_code") != 0:
            return None
        return data.get("result") or None

    async def fetch_overview(self, series_id: int) -> dict | None:
        """작품 개요(content) — 연재 요일(pub_period "월, 목"), 판매 상태(state) 등. 요일별 목록에 없는 작품의 요일을 알아내는 데 쓴다. 못 받으면 None."""
        _, data = await self._request_json("GET", OVERVIEW_URL, params={"series_id": series_id}, label=f"작품 개요 series_id={series_id}")
        if not data or data.get("result_code") != 0:
            return None
        return (data.get("result") or {}).get("content") or None

    async def viewer_image_urls(self, series_id: int, product_id: int) -> list[str] | None:
        status, data = await self._request_json(
            "GET", VIEWER_DATA_URL, params={"series_id": series_id, "product_id": product_id},
            label=f"이미지 목록 product_id={product_id}",
        )
        if not data:
            # 서버가 이 회차를 열어 주지 않은 경우(HTTP 403 등)는 예전에는 아무 로그도 남지 않았다 — 상태를 원인으로 남긴다(요청 예외는 _request_json이 이미 기록)
            self.last_failure = f"이미지 목록 요청 실패(HTTP {status})" if status else "이미지 목록 요청 실패(네트워크 오류)"
            if status:
                log.warning("카카오페이지 이미지 목록을 받지 못했습니다 (series_id=%s, product_id=%s): HTTP %s", series_id, product_id, status)
            return None
        files = (((data.get("viewer_data") or {}).get("imageDownloadData") or {}).get("files")) or []
        urls = [f["secureUrl"] for f in sorted(files, key=lambda f: f.get("no", 0)) if f.get("secureUrl")]
        if not urls:
            self.last_failure = f"이미지 목록이 비어 있음(result_code={data.get('result_code')})"
            log.warning("카카오페이지 이미지 목록이 비어 있습니다 (series_id=%s, product_id=%s): result_code=%s", series_id, product_id, data.get("result_code"))
            return None
        return urls

    async def download_image(self, url: str) -> tuple[bytes, str] | None:
        """이미지 하나를 (바이트, Content-Type)으로. 실패하면 잠깐 쉬고 몇 번 더 시도하고, 그래도 안 되면 None.
        서명된 주소(token/signature)는 한 글자만 달라져도 404가 되어서, 주소를 다시 인코딩하지 않고 받은 그대로 보낸다."""
        self._ensure_current()
        target = URL(url, encoded=True)
        for attempt in range(_IMAGE_RETRIES):
            self._ensure_current()
            request_revision = self.cookie_revision
            try:
                async with self._session.get(target, headers=self._headers(image=True, url=url), timeout=self._timeout,
                                             allow_redirects=False) as response:
                    self._ensure_current(refresh=False)
                    if response.status == 200:
                        body = await response.read()
                        self._ensure_current(refresh=False)
                        if body:
                            if self.cookie_revision == request_revision:
                                self._absorb_cookies(response, request_url=url)
                                persist_refreshed_cookies(self, strict_auth=True)
                            return body, response.headers.get("Content-Type", "")
                    else:
                        log.warning("카카오페이지 이미지 받기 실패: HTTP %s (%s)", response.status, target.host)
                        self.last_failure = f"이미지 받기 실패(HTTP {response.status})"
            except kakao_page_auth.KakaoAuthPending:
                raise
            except Exception as e:
                log.warning("카카오페이지 이미지 받기 예외: %s", type(e).__name__)
                self.last_failure = f"이미지 받기 실패({type(e).__name__})"
            if attempt < _IMAGE_RETRIES - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
        return None


def client_from_saved_cookies(session: aiohttp.ClientSession, timeout_seconds: int) -> KakaoPageClient | None:
    """저장된 로그인 쿠키로 클라이언트를 만든다. 저장된 쿠키가 없으면 None."""
    cookies, generation, revision = kakao_page_auth.load_state()
    if not cookies:
        return None
    return KakaoPageClient(session, kakao_page_auth.cookie_map(cookies), timeout_seconds, cookie_entries=cookies, credential_version=generation, cookie_revision=revision)


def new_session() -> aiohttp.ClientSession:
    """카카오페이지 호출용 세션 — 쿠키는 KakaoPageClient가 직접 관리하므로 세션엔 쿠키 저장소를 두지 않는다."""
    return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())


def client_or_anonymous(session: aiohttp.ClientSession, timeout_seconds: int) -> KakaoPageClient:
    """저장된 로그인 쿠키가 있으면 그걸로, 없으면 쿠키 없이 부르는 클라이언트(작품 정보처럼 로그인이 필요 없는 조회용)."""
    return client_from_saved_cookies(session, timeout_seconds) or KakaoPageClient(session, {}, timeout_seconds)


def persist_refreshed_cookies(client: KakaoPageClient, *, strict_auth=False) -> None:
    """서버가 로그인 유지를 위해 새로 내려준 쿠키가 있으면 저장해서, 다음 실행에도 이어지게 한다."""
    if client.cookies_changed:
        if client.credential_version is None:
            return
        result = kakao_page_auth.update_cookies(client.cookie_entries, client.credential_version,
                                               base_cookies=client.persisted_cookie_entries, strict_auth=strict_auth)
        if result is None:
            raise kakao_page_auth.KakaoAuthPending('credentials_changed', 'credential_version_changed', client.credential_version)
        cookies, revision = result
        client.cookie_entries = [dict(c) for c in cookies]
        client.persisted_cookie_entries = [dict(c) for c in cookies]
        client.cookies = kakao_page_auth.cookie_map(cookies)
        client.cookie_revision = revision
        client.cookies_changed = False
