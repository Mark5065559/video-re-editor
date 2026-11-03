"""End-to-end bot scenarios with a fake Telegram server (no network, no real token)."""
import asyncio
import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetFile, SendMessage, SendVideo
from aiogram.types import File, Message, Update

import app.handlers as handlers
from app.config import Settings
from app.db import HistoryDB
from app.handlers import BotServices, setup_router
from app.media import MediaError

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg не установлен"
)

TOKEN = "123456789:" + "A" * 35
USER = {"id": 777, "is_bot": False, "first_name": "Owner"}
CHAT = {"id": 777, "type": "private"}
MB = 1024 * 1024


class FakeTelegram(BaseSession):
    def __init__(self, files: dict[str, Path]):
        super().__init__()
        self.files = files
        self.calls: list = []
        self.videos: list[dict] = []
        self._next_id = 5000

    def message(self, bot: Bot, **fields) -> Message:
        self._next_id += 1
        data = {"message_id": self._next_id, "date": int(time.time()), "chat": CHAT,
                "from": {"id": 123456789, "is_bot": True, "first_name": "Bot"}}
        data.update({key: value for key, value in fields.items() if value is not None})
        return Message.model_validate(data, context={"bot": bot})

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetFile):
            path = self.files[method.file_id]
            return File(file_id=method.file_id, file_unique_id="u" + method.file_id,
                        file_size=path.stat().st_size, file_path=f"videos/{method.file_id}")
        if isinstance(method, SendVideo):
            path = Path(method.video.path)
            self.videos.append({
                "path": path, "sha": hashlib.sha256(path.read_bytes()).hexdigest(),
                "width": method.width, "height": method.height, "caption": method.caption,
                "thumb": method.thumbnail is not None,
            })
            return self.message(bot, caption=method.caption)
        if isinstance(method, SendMessage):
            return self.message(bot, text=method.text)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        with open(self.files[url.rsplit("/", 1)[-1]], "rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk

    async def close(self):
        pass

    def texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText))]

    def alerts(self) -> list[str]:
        return [c.text or "" for c in self.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]


class FakeMTProto:
    available = True

    def __init__(self, source: Path):
        self.source = source
        self.downloads: list[int] = []
        self.sent: list[dict] = []

    async def download(self, message_id, destination, progress=None):
        self.downloads.append(message_id)
        shutil.copyfile(self.source, destination)
        if progress:
            await progress(destination.stat().st_size, destination.stat().st_size)

    async def send_video(self, **kwargs):
        self.sent.append(dict(kwargs, exists=Path(kwargs["path"]).exists()))


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        bot_token=TOKEN, bot_api_mode="official", bot_api_base_url="https://api.telegram.org",
        max_input_mb=19, max_output_mb=47, max_duration_seconds=600, data_dir=tmp_path / "data",
        max_parallel_jobs=1, job_timeout_seconds=120, user_rate_limit_count=10,
        user_rate_limit_window_seconds=600,
    )
    values.update(overrides)
    return Settings(**values)


def video_update(bot: Bot, update_id: int, message_id: int, file_id: str, size: int, *, document=False):
    media = {"file_id": file_id, "file_unique_id": "u" + file_id, "file_size": size}
    message = {"message_id": message_id, "date": int(time.time()), "chat": CHAT, "from": USER}
    if document:
        message["document"] = dict(media, file_name="clip.mov", mime_type="video/quicktime")
    else:
        message["video"] = dict(media, width=640, height=360, duration=3, file_name="clip.mp4",
                                mime_type="video/mp4")
    return Update.model_validate({"update_id": update_id, "message": message}, context={"bot": bot})


def text_update(bot: Bot, update_id: int, text: str, **extra):
    message = {"message_id": 50 + update_id, "date": int(time.time()), "chat": CHAT, "from": USER, "text": text}
    message.update(extra)
    return Update.model_validate({"update_id": update_id, "message": message}, context={"bot": bot})


def callback_update(bot: Bot, update_id: int, data: str):
    query = {"id": str(update_id), "from": USER, "chat_instance": "ci", "data": data,
             "message": {"message_id": 4000, "date": int(time.time()), "chat": CHAT, "text": "panel"}}
    return Update.model_validate({"update_id": update_id, "callback_query": query}, context={"bot": bot})


async def wait_jobs(services: BotServices) -> None:
    while services._tasks:
        await asyncio.gather(*list(services._tasks), return_exceptions=True)
    await asyncio.sleep(0)


def build(tmp_path: Path, files: dict[str, Path], mtproto=None, **settings_overrides):
    session = FakeTelegram(files)
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    settings = make_settings(tmp_path, **settings_overrides)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    services = BotServices(settings, HistoryDB(settings.db_path), mtproto)
    dp = Dispatcher()
    setup_router(dp, services, settings)
    return session, bot, services, dp


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path


def test_unique_flow_double_tap_reuse_and_cancel(tmp_path, sample):
    async def scenario():
        session, bot, services, dp = build(tmp_path, {"vid1": sample})
        await dp.feed_update(bot, video_update(bot, 1, 100, "vid1", sample.stat().st_size))
        assert "Видео проверено" in session.texts()[-1]
        pending = services.jobs.get(777)
        assert pending is not None and pending.source.exists()

        for update_id, data in enumerate(["uq:open", "uq:cnt:2", "uq:int:strong", "uq:fmt"], start=2):
            await dp.feed_update(bot, callback_update(bot, update_id, data))
        assert pending.unique.copies == 2 and pending.unique.intensity == "strong" and pending.unique.fmt == "9:16"
        assert "Уникализатор" in session.texts()[-1]

        # Double tap on "Run": only one job may start.
        await asyncio.gather(
            dp.feed_update(bot, callback_update(bot, 10, "uq:go")),
            dp.feed_update(bot, callback_update(bot, 11, "uq:go")),
        )
        await wait_jobs(services)
        assert len(session.videos) == 2
        assert {(v["width"], v["height"]) for v in session.videos} == {(1080, 1920)}
        assert session.videos[0]["sha"] != session.videos[1]["sha"]
        assert all(v["thumb"] and "Уникализатор" in v["caption"] for v in session.videos)
        assert any("Уже обрабатываю" in text or "идёт обработка" in text for text in session.alerts())
        assert "Готово" in session.texts()[-1]

        # The source stays for another variant without a new upload.
        assert services.jobs.get(777) is pending and pending.source.exists()
        await dp.feed_update(bot, callback_update(bot, 12, "preset:square"))
        await wait_jobs(services)
        assert (session.videos[-1]["width"], session.videos[-1]["height"]) == (1080, 1080)
        assert len(services.db.recent(777)) == 3
        assert not any(v["path"].exists() for v in session.videos)  # results removed after sending

        await dp.feed_update(bot, callback_update(bot, 13, "job:cancel"))
        assert services.jobs.get(777) is None and not pending.job_dir.exists()
        await bot.session.close()

    asyncio.run(scenario())


def test_failure_keeps_source_for_retry(tmp_path, sample, monkeypatch):
    async def broken(*args, **kwargs):
        raise MediaError("тестовая ошибка FFmpeg")

    monkeypatch.setattr(handlers, "process_video", broken)

    async def scenario():
        session, bot, services, dp = build(tmp_path, {"vid1": sample})
        await dp.feed_update(bot, video_update(bot, 1, 100, "vid1", sample.stat().st_size, document=True))
        await dp.feed_update(bot, callback_update(bot, 2, "preset:keep"))
        await wait_jobs(services)
        assert any("тестовая ошибка FFmpeg" in text for text in session.texts())
        assert "Исходник сохранён" in session.texts()[-1]
        pending = services.jobs.get(777)
        assert pending is not None and pending.source.exists()
        assert not session.videos
        await bot.session.close()

    asyncio.run(scenario())


def test_running_job_can_be_stopped(tmp_path, sample, monkeypatch):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handlers, "process_video", slow)

    async def scenario():
        session, bot, services, dp = build(tmp_path, {"vid1": sample})
        await dp.feed_update(bot, video_update(bot, 1, 100, "vid1", sample.stat().st_size))
        await dp.feed_update(bot, callback_update(bot, 2, "preset:reels_fill"))
        await asyncio.wait_for(started.wait(), 10)
        await dp.feed_update(bot, callback_update(bot, 3, "job:stop"))
        await asyncio.wait_for(wait_jobs(services), 10)
        assert any("остановлена" in text for text in session.texts())
        assert not services.jobs.is_active(777)
        assert services.jobs.get(777) is not None  # can choose another variant
        assert not session.videos
        await bot.session.close()

    asyncio.run(scenario())


def test_big_video_goes_through_mtproto(tmp_path, sample, monkeypatch):
    monkeypatch.setattr(handlers, "BOT_API_SAFE_UPLOAD_MB", 0)  # force the MTProto upload path

    async def scenario():
        mtproto = FakeMTProto(sample)
        session, bot, services, dp = build(tmp_path, {}, mtproto=mtproto, max_input_mb=300, max_output_mb=1000)
        assert services.input_limit_mb == 300
        await dp.feed_update(bot, video_update(bot, 1, 321, "big", 60 * MB))
        assert mtproto.downloads == [321]
        assert not any(isinstance(call, GetFile) for call in session.calls)
        await dp.feed_update(bot, callback_update(bot, 2, "preset:keep"))
        await wait_jobs(services)
        assert len(mtproto.sent) == 1 and mtproto.sent[0]["exists"]
        assert mtproto.sent[0]["message_id"] == 321 and mtproto.sent[0]["chat_id"] == 777
        assert not session.videos
        await bot.session.close()

    asyncio.run(scenario())


def test_big_video_without_mtproto_is_explained(tmp_path, sample):
    async def scenario():
        session, bot, services, dp = build(tmp_path, {}, max_input_mb=300)
        assert services.input_limit_mb == 20
        await dp.feed_update(bot, video_update(bot, 1, 100, "big", 60 * MB))
        assert "TG_API_ID" in session.texts()[-1]
        assert services.jobs.get(777) is None
        await bot.session.close()

    asyncio.run(scenario())


def test_private_bot_and_wrong_files(tmp_path, sample):
    async def scenario():
        session, bot, services, dp = build(tmp_path, {"vid1": sample}, allowed_user_ids=frozenset({1}))
        await dp.feed_update(bot, video_update(bot, 1, 100, "vid1", sample.stat().st_size))
        assert "частный бот" in session.texts()[-1] and "777" in session.texts()[-1]
        assert services.jobs.get(777) is None
        await bot.session.close()

        session, bot, services, dp = build(tmp_path / "public", {"vid1": sample})
        doc = {"file_id": "pdf", "file_unique_id": "upd", "file_size": 1000,
               "file_name": "report.pdf", "mime_type": "application/pdf"}
        await dp.feed_update(bot, text_update(bot, 2, None, document=doc))
        assert "не похоже на видео" in session.texts()[-1]
        await dp.feed_update(bot, text_update(bot, 3, "/id"))
        assert "777" in session.texts()[-1]
        await dp.feed_update(bot, text_update(bot, 4, "привет"))
        assert "Пришли видео" in session.texts()[-1]
        await dp.feed_update(bot, callback_update(bot, 5, "uq:go"))
        assert any("пришли видео заново" in text for text in session.alerts())
        await bot.session.close()

    asyncio.run(scenario())


def test_first_user_becomes_owner_others_are_refused(tmp_path, sample):
    async def scenario():
        session, bot, services, dp = build(tmp_path, {"vid1": sample})
        assert services.settings.access_mode == "owner" and not services.owner_store.owners
        await dp.feed_update(bot, text_update(bot, 1, "/start"))
        assert any("владелец этого бота" in text for text in session.texts())
        assert services.owner_store.owners == {777}

        stranger = {"id": 888, "is_bot": False, "first_name": "Stranger"}
        update = Update.model_validate({"update_id": 2, "message": {
            "message_id": 90, "date": int(time.time()), "chat": {"id": 888, "type": "private"},
            "from": stranger, "text": "/start"}}, context={"bot": bot})
        await dp.feed_update(bot, update)
        assert "частный бот" in session.texts()[-1] and "888" in session.texts()[-1]
        assert services.owner_store.owners == {777}

        await dp.feed_update(bot, video_update(bot, 3, 100, "vid1", sample.stat().st_size))
        assert "Видео проверено" in session.texts()[-1]  # the owner keeps working
        await bot.session.close()

    asyncio.run(scenario())


class LazyMTProto(FakeMTProto):
    available = False

    def __init__(self, source):
        super().__init__(source)
        self.starts = 0

    async def start(self):
        self.starts += 1
        self.available = True
        return True


def test_cloud_mode_lazy_mtproto_lite_profile_and_owner_restore(tmp_path, sample):
    from aiogram.methods import GetMyCommands, SetMyCommands
    from aiogram.types import BotCommand

    async def scenario():
        mtproto = LazyMTProto(sample)
        session, bot, services, dp = build(tmp_path, {"vid1": sample}, mtproto=None, max_input_mb=300,
                                           max_output_mb=1000, cpu_profile="lite")
        services.mtproto, services.mtproto_lazy = mtproto, True
        assert services.big_files and services.input_limit_mb == 300 and services.profile.name == "lite"

        # Owner restored from Telegram after the disk was wiped.
        original = session.make_request

        async def with_commands(bot_, method, timeout=None):
            if isinstance(method, GetMyCommands):
                session.calls.append(method)
                return [BotCommand(command="vreowner", description="777")]
            return await original(bot_, method, timeout)

        session.make_request = with_commands
        await services.restore_owners(bot)
        assert services.owner_store.owners == {777}

        await dp.feed_update(bot, video_update(bot, 1, 321, "big", 60 * MB))
        assert mtproto.starts == 1 and mtproto.downloads == [321]  # logged in only when needed
        assert not any("владелец этого бота" in text for text in session.texts())  # already owned
        assert services.busy()  # a pending upload keeps a cloud instance awake
        await dp.feed_update(bot, callback_update(bot, 2, "preset:reels_fill"))
        await wait_jobs(services)
        assert (session.videos[-1]["width"], session.videos[-1]["height"]) == (720, 1280)  # lite size
        await dp.feed_update(bot, callback_update(bot, 3, "job:cancel"))
        assert not services.busy()
        await bot.session.close()

        # A fresh bot without stored owner: the first user claims it and it is saved on Telegram.
        session2, bot2, services2, dp2 = build(tmp_path / "fresh", {"vid1": sample})
        await dp2.feed_update(bot2, text_update(bot2, 1, "/start"))
        await wait_jobs(services2)
        saved = [c for c in session2.calls if isinstance(c, SetMyCommands) and c.language_code == "zu"]
        assert saved and saved[-1].commands[0].description == "777"
        await bot2.session.close()

    asyncio.run(scenario())
