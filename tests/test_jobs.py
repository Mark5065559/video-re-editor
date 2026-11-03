import asyncio
import time
from pathlib import Path

from app.jobs import JobManager, PendingUpload, RateLimiter, clean_temp_root
from app.media import VideoInfo

INFO = VideoInfo(duration=5.0, width=640, height=360, has_audio=True, video_codec="h264",
                 audio_codec="aac", size_bytes=10)


def make_pending(manager: JobManager, user_id: int = 1) -> PendingUpload:
    job_dir = manager.new_dir(user_id)
    source = job_dir / "source.mp4"
    source.write_bytes(b"video")
    return PendingUpload(user_id=user_id, chat_id=user_id, message_id=10, job_dir=job_dir,
                         source=source, filename="a.mp4", info=INFO)


def test_pending_can_be_taken_only_once(tmp_path: Path):
    manager = JobManager(tmp_path, ttl_seconds=60)
    pending = make_pending(manager)
    manager.set_pending(pending)
    assert manager.take(1) is pending
    assert manager.take(1) is None  # a double tap gets nothing


def test_source_is_kept_after_job_unless_newer_upload_exists(tmp_path: Path):
    manager = JobManager(tmp_path, ttl_seconds=60)
    first = make_pending(manager)
    manager.set_pending(first)
    manager.take(1)
    assert manager.give_back(first) and first.source.exists() and manager.get(1) is first

    manager.take(1)
    second = make_pending(manager)
    manager.set_pending(second)  # user uploaded a new video while the job was running
    assert not manager.give_back(first)
    assert not first.job_dir.exists() and second.source.exists() and manager.get(1) is second


def test_new_upload_replaces_old_pending(tmp_path: Path):
    manager = JobManager(tmp_path, ttl_seconds=60)
    first, second = make_pending(manager), make_pending(manager)
    manager.set_pending(first)
    manager.set_pending(second)
    assert not first.job_dir.exists() and second.job_dir.exists()
    assert manager.drop(1) and not second.job_dir.exists() and not manager.drop(1)


def test_expired_pending_is_removed(tmp_path: Path):
    manager = JobManager(tmp_path, ttl_seconds=60)
    pending = make_pending(manager)
    manager.set_pending(pending)
    assert manager.expire(now=time.monotonic() + 30) == 0
    assert manager.expire(now=time.monotonic() + 61) == 1
    assert not pending.job_dir.exists() and manager.get(1) is None


def test_rate_limiter():
    limiter = RateLimiter(2, 60)
    assert limiter.hit(1) and limiter.hit(1) and not limiter.hit(1)
    assert limiter.hit(2)


def test_running_job_can_be_cancelled(tmp_path: Path):
    async def scenario():
        manager = JobManager(tmp_path, ttl_seconds=60)
        task = asyncio.create_task(asyncio.sleep(30))
        manager.start(5, task)
        assert manager.is_active(5) and not manager.cancel_requested(5)
        assert manager.request_cancel(5) and manager.cancel_requested(5)
        try:
            await task
        except asyncio.CancelledError:
            pass
        manager.finish(5)
        assert not manager.is_active(5) and not manager.request_cancel(5)

    asyncio.run(scenario())


def test_temp_root_is_cleaned(tmp_path: Path):
    (tmp_path / "1_abc").mkdir()
    (tmp_path / "1_abc" / "source.mp4").write_bytes(b"x")
    (tmp_path / "stray.tmp").write_bytes(b"x")
    assert clean_temp_root(tmp_path) == 1
    assert list(tmp_path.iterdir()) == []
