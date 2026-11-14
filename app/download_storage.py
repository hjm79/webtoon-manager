"""Read the filesystem capacity behind a download folder without scanning files."""
import asyncio
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time

from app.path_guard import PathOutsideRootError

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download-storage")
_lock = threading.Lock()
_pending: dict[tuple[str, str | None], Future] = {}
_cache: OrderedDict[tuple[str, str | None], tuple[float, dict]] = OrderedDict()
_CACHE_SECONDS = 5.0
_LOOKUP_SECONDS = 2.0


def _state(path: str, status: str, filesystem_type: str | None = None) -> dict:
    return {"status": status, "filesystem_id": None, "total_bytes": None, "used_bytes": None,
            "available_bytes": None, "read_only": None, "checked_path": path,
            "filesystem_type": filesystem_type}


def _mount_for(path: str) -> dict | None:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except FileNotFoundError:
        return None  # Native macOS and other non-proc platforms.
    matches = []
    target = Path(path)
    for line in lines:
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 6 or not filesystem:
            continue
        point = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if target == point or point in target.parents:
            matches.append({"point": str(point), "type": filesystem[0]})
    return max(matches, key=lambda item: len(item["point"])) if matches else None


def _mount_problem(path: str) -> dict | None:
    mount = _mount_for(path)
    if mount is None:
        return None
    kind = mount["type"]
    if kind == "fuse" or kind.startswith("fuse.") or kind in {"nfs", "nfs4", "cifs", "smb3", "9p", "ceph", "glusterfs"}:
        return _state(path, "remote", kind)
    if kind in {"overlay", "aufs", "rootfs"}:
        return _state(path, "not_mounted", kind)
    return None


def _read_capacity(path: str, allowed_root: str | None = None) -> dict:
    try:
        problem = _mount_problem(path)
        if problem is not None and problem["status"] == "remote":
            return problem
        target = Path(path).resolve()
        if allowed_root is not None:
            root = Path(allowed_root).resolve()
            if target != root and root not in target.parents:
                raise PathOutsideRootError("경로가 다운로드 루트 밖입니다.")
        problem = _mount_problem(str(target))
        if problem is not None:
            return problem
        info = target.stat()
        if not stat.S_ISDIR(info.st_mode):
            return _state(str(target), "unavailable")
        usage = os.statvfs(target)
        block = usage.f_frsize or usage.f_bsize
        total = usage.f_blocks * block
        if total <= 0:
            return _state(str(target), "unavailable")
        mount = _mount_for(str(target))
        result = _state(str(target), "ok", mount["type"] if mount else None)
        result.update(filesystem_id=f"device:{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                      total_bytes=total, used_bytes=max(0, min(total, (usage.f_blocks - usage.f_bfree) * block)),
                      available_bytes=max(0, min(total, usage.f_bavail * block)),
                      read_only=bool(usage.f_flag & getattr(os, "ST_RDONLY", 1)))
        return result
    except FileNotFoundError:
        return _state(path, "missing")
    except PermissionError:
        return _state(path, "permission_denied")
    except OSError:
        return _state(path, "unavailable")


def _completed(key: tuple[str, str | None], future: Future) -> None:
    try:
        result = future.result()
    except PathOutsideRootError:
        result = None
    except Exception as error:
        log.warning("Download storage lookup failed (%s)", type(error).__name__)
        result = _state(key[0], "unavailable")
    with _lock:
        _pending.pop(key, None)
        if result is not None:
            _cache[key] = (time.monotonic() + _CACHE_SECONDS, result)
            _cache.move_to_end(key)
            while len(_cache) > 64:
                _cache.popitem(last=False)


async def capacity(path: str, *, allowed_root: str | None = None) -> dict:
    """Bound outstanding OS calls; a timed-out syscall is reused rather than duplicated."""
    key = (os.path.abspath(path), allowed_root)
    created = False
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return dict(cached[1])
        future = _pending.get(key)
        if future is None:
            if len(_pending) >= 2:
                return _state(key[0], "unavailable")
            future = _executor.submit(_read_capacity, key[0], allowed_root)
            _pending[key] = future
            created = True
    if created:
        future.add_done_callback(lambda done: _completed(key, done))
    try:
        result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=_LOOKUP_SECONDS)
        return dict(result)
    except asyncio.TimeoutError:
        return _state(key[0], "timeout")
    except PathOutsideRootError:
        raise
    except Exception as error:
        log.warning("Download storage lookup failed (%s)", type(error).__name__)
        return _state(key[0], "unavailable")
