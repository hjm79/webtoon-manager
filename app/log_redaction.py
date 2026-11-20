"""
로그에 비밀이 남지 않게 마스킹한다.

- 디스코드 웹훅 URL / 봇 토큰: 설정에 저장된 값이 메시지에 그대로 들어 있으면 가린다.
- 카카오페이지 서명 주소: 이미지 주소의 token / signature / credential 쿼리 값(이 값만 알면 누구나 그 이미지를 받을 수 있다).
- 카카오페이지 로그인 쿠키: 저장된 쿠키 값이 메시지에 들어 있으면 가린다(예외 메시지에 요청 헤더가 섞이는 경우 대비).

비밀 목록을 읽다가 실패해도 로그 자체는 막지 않는다(로그가 사라지는 것보다 마스킹이 덜 되는 편이 낫다 — 그래도 주소 마스킹은 항상 한다).
"""

import logging
import re
from threading import local

from app import discord_config, kakao_page_auth

_MASK = "***REDACTED***"
_MIN_SECRET_LENGTH = 10  # 이보다 짧은 값은 일반 문장과 겹쳐 오탐이 나기 쉬워서 값 비교로는 가리지 않는다
_redaction_context = local()
# 이름이 정확히 token/signature/credential인 쿼리 매개변수만(예: mytoken=은 제외 — 앞이 단어 경계여야 한다)
_QUERY_SECRET_RE = re.compile(r"(?i)\b(token|signature|credential)=([^&\s\"'<>;)]+)")


def _secret_values() -> list[str]:
    """지금 저장돼 있는 비밀 값들(디스코드 웹훅/봇 토큰, 카카오 로그인 쿠키 값). 매번 현재 값을 조회한다."""
    secrets = [discord_config.get_bot_token(), discord_config.get_webhook_url()]
    cookies = kakao_page_auth.load_cookies()
    if cookies:
        secrets.extend(kakao_page_auth.cookie_map(cookies).values())
    return [s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH]


def _available_secrets() -> list[str]:
    if getattr(_redaction_context, "loading", False):
        return []
    _redaction_context.loading = True
    try:
        return _secret_values()
    except Exception:
        return []
    finally:
        _redaction_context.loading = False


def _redact(message: str, secrets: list[str]) -> str:
    message = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", message)
    for secret in secrets:
        if secret in message:
            message = message.replace(secret, _MASK)
    return message


def redact(message: str) -> str:
    return _redact(message, _available_secrets())


def redact_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    secrets = _available_secrets()
    return [_redact(line, secrets) for line in lines]


class RedactingFilter(logging.Filter):
    """로그 레코드의 최종 메시지(인자 반영 후)를 마스킹한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True
