"""Interest discovery and explicit, atomic admission to automatic subscriptions."""
import json

from app import repository
from app.db import read_lock, write_transaction
from app.repository.common import SOURCE_ARTIST, SOURCE_TAG, STATUS_ACTIVE, _now

ENABLED_KEY = 'auto_subscribe_enabled'
SCOPE_KEY = 'auto_subscribe_scope'
DISCOVERIES_KEY = 'registry_discoveries'


def is_enabled() -> bool:
    return repository.get_setting(ENABLED_KEY) == '1'


def validate_scope(value: dict) -> dict:
    if not isinstance(value, dict) or value.get('mode') not in ('future', 'latest', 'all'):
        raise ValueError('자동 구독 범위는 이후 새 회차·최근 회차·전체 중 선택해주세요.')
    count = value.get('latest_count')
    if value['mode'] == 'latest' and (type(count) is not int or not 1 <= count <= 1000):
        raise ValueError('최근 회차 수는 1~1000 사이여야 합니다.')
    return {'mode': value['mode'], 'latest_count': count if value['mode'] == 'latest' else None}


def _value(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _scope_in(conn) -> dict:
    raw = _value(conn, SCOPE_KEY)
    return validate_scope(json.loads(raw)) if raw else {'mode': 'future', 'latest_count': None}


def scope_config() -> dict:
    with read_lock() as conn:
        return _scope_in(conn)


def _put(conn, key, value):
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def set_config(enabled: bool, scope: dict | None = None) -> None:
    policy = validate_scope(scope) if scope is not None else None
    with write_transaction() as conn:
        if policy is not None:
            _put(conn, SCOPE_KEY, json.dumps(policy))
        _put(conn, ENABLED_KEY, '1' if enabled else '0')


def set_enabled(enabled: bool) -> None:
    set_config(enabled)


def _selected(conn, platform, source, source_id):
    if platform not in ('naver', 'kakao') or source not in (SOURCE_ARTIST, SOURCE_TAG):
        return False
    if platform == 'kakao' and _value(conn, 'kakao_webtoons_enabled') != '1':
        return False
    if source == SOURCE_TAG:
        row = conn.execute('SELECT enabled FROM watched_tags WHERE tag_id=?', (source_id,)).fetchone() if platform == 'naver' else None
    else:
        row = conn.execute('SELECT enabled FROM watched_authors WHERE platform=? AND author_id=?', (platform, source_id)).fetchone()
    return bool(row and row[0])


def _tracked(conn, platform, title_id):
    table = 'webtoons' if platform == 'naver' else 'kakao_webtoons'
    return conn.execute(f'SELECT 1 FROM {table} WHERE title_id=?', (title_id,)).fetchone() is not None


def observe(platform: str, source: str, source_id: str, source_name: str, items: list[dict]) -> list[dict]:
    """Baseline the first successful lookup; retain discoveries for manual review/retry."""
    key = f'registry_seen:{platform}:{source}:{source_id}'
    current = {str(item['title_id']): item for item in items}
    with write_transaction() as conn:
        if not _selected(conn, platform, source, source_id):
            return []
        raw = _value(conn, key)
        legacy = {str(r[0]) for r in conn.execute('SELECT title_id FROM kakao_seen_titles WHERE author_name=?', (source_id,))} if platform == 'kakao' else set()
        seen = set(json.loads(raw)) if raw is not None else legacy
        records = json.loads(_value(conn, DISCOVERIES_KEY) or '[]')
        if raw is not None or legacy:
            for title_id in sorted(current.keys() - seen):
                if _tracked(conn, platform, title_id):
                    continue
                entry = next((r for r in records if r['platform'] == platform and r['title_id'] == title_id), None)
                if entry is None:
                    entry = {'platform': platform, 'title_id': title_id, 'title': current[title_id]['title'], 'discovered_at': _now(), 'sources': []}
                    records.append(entry)
                origin = {'kind': source, 'id': source_id, 'name': source_name}
                if origin not in entry['sources']:
                    entry['sources'].append(origin)
        _put(conn, key, json.dumps(sorted(seen | current.keys())))
        if platform == 'kakao':
            conn.executemany('INSERT OR IGNORE INTO kakao_seen_titles(author_name,title_id,title_name,seen_at) VALUES (?,?,?,?)',
                             [(source_id, int(tid), item['title'], _now()) for tid, item in current.items()])
        records = records[-500:]
        _put(conn, DISCOVERIES_KEY, json.dumps(records, ensure_ascii=False))
        return [r for r in records if r['platform'] == platform and r['title_id'] in current
                and not _tracked(conn, platform, r['title_id'])
                and any(s['kind'] == source and s['id'] == source_id for s in r['sources'])]


def discoveries() -> list[dict]:
    with read_lock() as conn:
        records = json.loads(_value(conn, DISCOVERIES_KEY) or '[]')
        return [r for r in reversed(records) if not _tracked(conn, r['platform'], r['title_id'])
                and any(_selected(conn, r['platform'], s['kind'], s['id']) for s in r['sources'])]


def _admit(conn, platform, title_id, title, source, source_ids, scope, policy, thumbnail_url='', author_summary=''):
    if (_value(conn, ENABLED_KEY) != '1' or not source_ids or policy != _scope_in(conn)
            or not scope or scope.get('explicit') is not True or scope.get('mode') != policy['mode']
            or scope.get('latest_count') != policy['latest_count']
            or not any(_selected(conn, platform, source, sid) for sid in source_ids)):
        return False
    now = _now()
    if platform == 'naver':
        cursor = conn.execute(
            'INSERT OR IGNORE INTO webtoons(title_id,title,status,added_source,ever_subscribed,created_at,updated_at) VALUES (?,?,?,?,1,?,?)',
            (title_id, title, STATUS_ACTIVE, source, now, now))
    else:
        cursor = conn.execute(
            'INSERT OR IGNORE INTO kakao_webtoons(title_id,title,status,ever_subscribed,thumbnail_url,author_summary,created_at,updated_at) VALUES (?,?,?,1,?,?,?,?)',
            (title_id, title, STATUS_ACTIVE, thumbnail_url, author_summary, now, now))
    if cursor.rowcount != 1:
        return False
    _put(conn, f'subscription_scope:{platform}:{title_id}', json.dumps(scope))
    if platform == 'kakao':
        _put(conn, f'auto_subscription_source:kakao:{title_id}', source)
    return True


def add_naver(title_id: str, title: str, source: str, *, source_ids=(), scope=None, policy=None) -> bool:
    if source not in (SOURCE_ARTIST, SOURCE_TAG):
        raise ValueError('Automatic subscription source must be artist or tag')
    with write_transaction() as conn:
        return _admit(conn, 'naver', title_id, title, source, source_ids, scope, policy)


def add_kakao(title_id: int, title: str, thumbnail_url: str, author_summary: str, *, source_id='', scope=None, policy=None) -> bool:
    with write_transaction() as conn:
        return _admit(conn, 'kakao', title_id, title, SOURCE_ARTIST, [source_id] if source_id else [], scope, policy, thumbnail_url, author_summary)


def kakao_source(title_id: int) -> str:
    return repository.get_setting(f'auto_subscription_source:kakao:{title_id}') or 'unknown'
