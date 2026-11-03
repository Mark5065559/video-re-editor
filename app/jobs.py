"""Per-user bookkeeping of uploaded videos and running jobs.

Every uploaded video lives in its own folder. The folder belongs either to the
"pending" slot of the user (waiting for a preset choice) or to exactly one running
job, so a new upload, a double tap on a button or a cancel can never delete files
that FFmpeg is still using. All methods are synchronous: with asyncio this makes
every check-and-update atomic.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .media import VideoInfo
from .unique import UniqueOptions

log = logging.getLogger(__name__)


@dataclass
class PendingUpload:
    user_id: int
    chat_id: int
    message_id: int  # the user's message with the video (needed for MTProto)
    job_dir: Path
    source: Path
    filename: str
    info: VideoInfo
    unique: UniqueOptions = field(default_factory=UniqueOptions)
    touched: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched = time.monotonic()


@dataclass
class ActiveJob:
    task: asyncio.Task | None
    cancel_requested: bool = False


class RateLimiter:
    def __init__(self, count: int, window_seconds: int):
        self.count = count
        self.window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, user_id: int) -> bool:
        """Registers an attempt; False if the user is over the limit."""
        now = time.monotonic()
        bucket = self._hits[user_id]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.count:
            return False
        bucket.append(now)
        return True


def remove_dir(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


class JobManager:
    def __init__(self, temp_dir: Path, ttl_seconds: int):
        self.temp_dir = temp_dir
        self.ttl_seconds = ttl_seconds
        self._pending: dict[int, PendingUpload] = {}
        self._active: dict[int, ActiveJob] = {}

    # --- folders -------------------------------------------------------------
    def new_dir(self, user_id: int) -> Path:
        path = self.temp_dir / f"{user_id}_{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    # --- pending uploads -----------------------------------------------------
    def get(self, user_id: int) -> PendingUpload | None:
        pending = self._pending.get(user_id)
        if pending is not None:
            pending.touch()
        return pending

    def set_pending(self, pending: PendingUpload) -> None:
        old = self._pending.get(pending.user_id)
        self._pending[pending.user_id] = pending
        if old is not None and old.job_dir != pending.job_dir:
            remove_dir(old.job_dir)

    def take(self, user_id: int) -> PendingUpload | None:
        """Hands the pending upload over to a job; nobody else can take it now."""
        return self._pending.pop(user_id, None)

    def give_back(self, pending: PendingUpload) -> bool:
        """After a job: keep the source for another variant unless a newer upload exists."""
        current = self._pending.get(pending.user_id)
        if current is not None and current.job_dir != pending.job_dir:
            remove_dir(pending.job_dir)
            return False
        if not pending.source.exists():
            remove_dir(pending.job_dir)
            return False
        pending.touch()
        self._pending[pending.user_id] = pending
        return True

    def drop(self, user_id: int) -> bool:
        pending = self._pending.pop(user_id, None)
        if pending is None:
            return False
        remove_dir(pending.job_dir)
        return True

    def expire(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        expired = [
            user_id for user_id, pending in self._pending.items()
            if now - pending.touched > self.ttl_seconds
        ]
        for user_id in expired:
            self.drop(user_id)
        return len(expired)

    # --- running jobs --------------------------------------------------------
    def is_active(self, user_id: int) -> bool:
        return user_id in self._active

    def start(self, user_id: int, task: asyncio.Task | None) -> ActiveJob:
        job = ActiveJob(task=task)
        self._active[user_id] = job
        return job

    def finish(self, user_id: int) -> None:
        self._active.pop(user_id, None)

    def request_cancel(self, user_id: int) -> bool:
        job = self._active.get(user_id)
        if job is None or job.task is None or job.task.done():
            return False
        job.cancel_requested = True
        job.task.cancel()
        return True

    def cancel_requested(self, user_id: int) -> bool:
        job = self._active.get(user_id)
        return bool(job and job.cancel_requested)

    def active_count(self) -> int:
        return len(self._active)

    def pending_count(self) -> int:
        return len(self._pending)


def clean_temp_root(temp_dir: Path) -> int:
    """At start-up nothing is pending yet, so every leftover job folder is garbage."""
    removed = 0
    if not temp_dir.exists():
        return removed
    for folder in temp_dir.iterdir():
        try:
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
            else:
                folder.unlink(missing_ok=True)
        except OSError:
            log.exception("Не удалось очистить временную папку %s", folder)
    return removed
