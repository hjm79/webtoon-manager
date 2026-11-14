"""Keep a title's batch-rest deadline across cancellation and process recovery."""
import asyncio
from datetime import datetime, timedelta, timezone

from app import repository, workflow_progress


def _key(platform: str, title_id: str | int) -> str:
    return f"download_cooldown:{platform}:{title_id}"


def deadline(platform: str, title_id: str | int) -> str | None:
    value = repository.get_setting(_key(platform, title_id))
    if not value:
        return None
    try:
        until = datetime.fromisoformat(value)
        if until.tzinfo is None:
            return None
    except ValueError:
        return None
    return value


async def resume(platform: str, title_id: str | int) -> None:
    until = deadline(platform, title_id)
    if until is None:
        return
    remaining = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        workflow_progress.update(state="resting", rest_until=until)
        await asyncio.sleep(remaining)
    repository.set_setting(_key(platform, title_id), None)


async def rest(platform: str, title_id: str | int, seconds: float) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    repository.set_setting(_key(platform, title_id), until.isoformat())
    workflow_progress.update(state="resting", rest_until=until.isoformat())
    await asyncio.sleep(seconds)
    repository.set_setting(_key(platform, title_id), None)
