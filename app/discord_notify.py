"""
신작 자동추가 웹훅 알림.

완결 확인(구독해제/알람제외) 관련 로직은 discord_bot.py의 실시간 Gateway 봇으로
이전되었다 — 예전에는 REST 폴링 + 텍스트 명령 스레드였지만, 이제 discord.py
Gateway 연결로 완결 감지 즉시 버튼 메시지를 보내고 클릭을 바로 처리한다.
"""

import logging
import re
from urllib.parse import urlsplit

import aiohttp

from app import discord_config
from app.constants import DISCORD_MESSAGE_CHUNK_LIMIT

log = logging.getLogger(__name__)

_WEBHOOK_PATH = re.compile(r"/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9_-]+")


def valid_webhook_url(value: str) -> bool:
    try:
        url = urlsplit(value.strip())
    except ValueError:
        return False
    return (url.scheme == "https" and url.netloc.lower() in {"discord.com", "discordapp.com"}
            and _WEBHOOK_PATH.fullmatch(url.path) is not None and not (url.query or url.fragment))


def _split_message(text: str, limit: int = DISCORD_MESSAGE_CHUNK_LIMIT) -> list[str]:
    """한도(limit) 안에서 줄 단위로 나눈다. 디스코드는 한 메시지가 한도를 넘거나 비어 있으면 통째로 거부하므로, 빈 조각은 만들지 않고
    한 줄 자체가 한도보다 길면 그 줄을 한도 길이로 잘라서 나눈다."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        for piece in [line[i : i + limit] for i in range(0, len(line), limit)] or [""]:
            if current and len(current) + len(piece) + 1 > limit:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


async def send_webhook_notification(session: aiohttp.ClientSession, message: str) -> bool:
    """웹훅으로 보낸다. 모두 보냈으면 True, 웹훅이 없거나 보낼 내용이 없거나 하나라도 실패하면 False(알림 기록을 남길지 정하는 데 쓴다)."""
    webhook_url = discord_config.get_webhook_url()
    if not webhook_url or not message.strip():
        return False
    if not valid_webhook_url(webhook_url):
        log.warning("디스코드 웹훅 형식이 올바르지 않습니다.")
        return False
    all_sent = True
    for chunk in _split_message(message):
        try:
            async with session.post(webhook_url, json={"content": chunk}, allow_redirects=False) as resp:
                if resp.status != 204:
                    all_sent = False
                    log.error("웹훅 전송 실패: HTTP %s", resp.status)
        except Exception as e:
            all_sent = False
            log.error("웹훅 연결 실패 (%s)", type(e).__name__)
    return all_sent


async def send_test_webhook_message() -> tuple[bool, str]:
    """설정 페이지의 '웹훅 테스트' 버튼에서 호출."""
    webhook_url = discord_config.get_webhook_url()
    if not webhook_url:
        return False, "웹훅 URL이 설정되지 않았습니다."
    if not valid_webhook_url(webhook_url):
        return False, "디스코드 웹훅 URL 형식을 확인해주세요."
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url, json={"content": "✅ 웹훅 테스트 메시지입니다. 정상적으로 연결되어 있습니다."},
                allow_redirects=False,
            ) as resp:
                if resp.status == 204:
                    return True, "테스트 메시지를 전송했습니다."
                return False, f"전송 실패: HTTP {resp.status}"
    except Exception as e:
        log.warning("디스코드 웹훅 테스트 연결 실패 (%s)", type(e).__name__)
        return False, "디스코드 웹훅에 연결하지 못했습니다."
