from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.chat_action import ChatActionSender

from .access import OwnerStore
from .config import BOT_API_DOWNLOAD_LIMIT_MB, BOT_API_SAFE_UPLOAD_MB, MB, Settings
from .db import HistoryDB
from .jobs import JobManager, PendingUpload, RateLimiter, remove_dir
from .keyboards import main_keyboard, preset_keyboard, stop_keyboard, unique_keyboard
from .media import (
    PRESETS,
    FFmpegTimeoutError,
    MediaError,
    VideoInfo,
    make_thumbnail,
    probe_video,
    process_video,
    sha256_file,
)
from .mtproto import MTProtoError, MTProtoFiles
from .unique import (
    COPY_CHOICES,
    FORMATS,
    INTENSITIES,
    UniqueOptions,
    describe,
    generate_params,
    process_unique,
)

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".m2ts", ".ts",
    ".mpg", ".mpeg", ".flv", ".wmv",
}
MAX_PIXELS = 4096 * 2304  # up to 4K (UHD and DCI)
DOWNLOAD_TIMEOUT = 900  # seconds, HTTP Bot API download
UPLOAD_TIMEOUT = 900  # seconds, HTTP Bot API upload


class BotServices:
    def __init__(self, settings: Settings, db: HistoryDB, mtproto: MTProtoFiles | None = None):
        self.settings = settings
        self.db = db
        self.mtproto = mtproto
        self.jobs = JobManager(settings.temp_dir, ttl_seconds=settings.pending_ttl_minutes * 60)
        self.semaphore = asyncio.Semaphore(settings.max_parallel_jobs)
        self.upload_limiter = RateLimiter(settings.user_rate_limit_count, settings.user_rate_limit_window_seconds)
        self.job_limiter = RateLimiter(settings.user_rate_limit_count, settings.user_rate_limit_window_seconds)
        self.owner_store = OwnerStore(settings.owners_path)
        self._tasks: set[asyncio.Task] = set()

    @property
    def mtproto_ready(self) -> bool:
        return self.mtproto is not None and self.mtproto.available

    @property
    def big_files(self) -> bool:
        return self.settings.bot_api_mode == "local" or self.mtproto_ready

    @property
    def input_limit_mb(self) -> int:
        if self.big_files:
            return self.settings.max_input_mb
        return min(self.settings.max_input_mb, BOT_API_DOWNLOAD_LIMIT_MB)

    @property
    def output_limit_mb(self) -> int:
        if self.big_files:
            return self.settings.max_output_mb
        return min(self.settings.max_output_mb, 47)

    @property
    def access_description(self) -> str:
        if self.settings.allowed_user_ids:
            return "только ID из ALLOWED_USER_IDS"
        if self.settings.access_mode == "public":
            return "все пользователи"
        return "только владелец"

    def user_allowed(self, user_id: int) -> bool:
        if self.settings.allowed_user_ids:
            return user_id in self.settings.allowed_user_ids
        if self.settings.access_mode == "public":
            return True
        return user_id in self.owner_store.owners

    def claim_owner(self, user_id: int) -> bool:
        if self.settings.allowed_user_ids or self.settings.access_mode != "owner":
            return False
        if not self.owner_store.claim(user_id):
            return False
        log.info("Владелец бота назначен: %s", user_id)
        return True

    def allow_upload(self, user_id: int) -> bool:
        # A private bot (owner mode or ALLOWED_USER_IDS) has no rate limit for its users.
        return self.settings.is_private_bot or self.upload_limiter.hit(user_id)

    def allow_job(self, user_id: int) -> bool:
        return self.settings.is_private_bot or self.job_limiter.hit(user_id)

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cleanup_loop(self, interval: float = 60) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                removed = self.jobs.expire()
                if removed:
                    log.info("Удалено устаревших исходников: %s", removed)
            except Exception:
                log.exception("Ошибка очистки временных файлов")

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=15)


# --- small helpers ---------------------------------------------------------

def mb(size: int | float) -> str:
    return f"{size / MB:.1f} МБ"


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f} сек."
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} мин {rest:02d} сек."


def progress_bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


async def safe_edit(message: object, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    if not isinstance(message, Message):
        return False
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramRetryAfter:
        return False
    except TelegramBadRequest as exc:
        return "not modified" in str(exc).lower()
    except TelegramAPIError:
        log.debug("Не удалось изменить сообщение", exc_info=True)
        return False


async def safe_delete(message: object) -> None:
    if isinstance(message, Message):
        try:
            await message.delete()
        except TelegramAPIError:
            pass


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, FFmpegTimeoutError):
        return "обработка заняла слишком много времени и была остановлена. Попробуй более короткий ролик."
    if isinstance(exc, (MediaError, MTProtoError, ValueError)):
        return html.escape(str(exc)[:700])
    if isinstance(exc, TelegramAPIError):
        return "ошибка Telegram: " + html.escape(str(exc)[:300])
    return "внутренняя ошибка. Подробности — в окне, где запущен бот."


class ProgressReporter:
    """Edits a status message at most every few seconds without slowing the work down."""

    def __init__(self, message: Message, title: str, *, reply_markup: InlineKeyboardMarkup | None = None,
                 min_interval: float = 3.0):
        self.message = message
        self.title = title
        self.reply_markup = reply_markup
        self.min_interval = min_interval
        self._last_time = 0.0
        self._last_percent = -1
        self._task: asyncio.Task | None = None

    async def update(self, fraction: float, extra: str = "") -> None:
        percent = int(max(0.0, min(1.0, fraction)) * 100)
        now = time.monotonic()
        if percent == self._last_percent or now - self._last_time < self.min_interval:
            return
        if self._task is not None and not self._task.done():
            return
        self._last_time, self._last_percent = now, percent
        text = f"{self.title}\n{progress_bar(percent)} {percent}%{extra}"
        self._task = asyncio.create_task(safe_edit(self.message, text, self.reply_markup))

    async def on_bytes(self, done: int, total: int) -> None:
        if total:
            await self.update(done / total, f" · {mb(done)} из {mb(total)}")

    async def done(self) -> None:
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (asyncio.TimeoutError, TelegramAPIError):
                pass


def video_info_text(info: VideoInfo) -> str:
    fps = f"{info.fps:g}" if info.fps else "—"
    return (
        "<b>Видео проверено</b>\n"
        f"Разрешение: {info.width}×{info.height}\n"
        f"Длительность: {format_duration(info.duration)}\n"
        f"Размер: {mb(info.size_bytes)}\n"
        f"Кадров в секунду: {fps}\n"
        f"Звук: {'есть' if info.has_audio else 'нет'}\n\n"
        "Выбери вариант подготовки:"
    )


def unique_panel_text(options: UniqueOptions, info: VideoInfo) -> str:
    return (
        "<b>🎲 Уникализатор</b>\n"
        "Каждая копия получает свой случайный набор небольших изменений: зум и сдвиг кадра, "
        "микро-поворот, цвет, зерно, резкость, скорость, края по времени, звук, метаданные и параметры "
        "кодирования. Файл и картинка становятся технически другими, но гарантий, как площадки оценят "
        "ролик, нет.\n\n"
        f"Интенсивность: {INTENSITIES.get(options.intensity, options.intensity)}\n"
        f"Копий: {options.copies}\n"
        f"Зеркало: {'вкл' if options.mirror else 'выкл'} (переворачивает текст и логотипы)\n"
        f"Скорость: {'меняется на 1–6%' if options.speed else 'без изменений'}\n"
        f"Формат: {FORMATS.get(options.fmt, options.fmt)}\n"
        f"Исходник: {info.width}×{info.height}, {format_duration(info.duration)}\n\n"
        "Настрой и нажми «🚀 Запустить»."
    )


def mode_title(key: str) -> str:
    if key.startswith("unique:"):
        level = key.split(":", 1)[1]
        return f"Уникализатор · {INTENSITIES.get(level, level)}"
    preset = PRESETS.get(key)
    return preset.title if preset else key


def extract_media(message: Message) -> tuple[Any, str, str]:
    if message.video:
        media = message.video
        return media, media.file_name or "video.mp4", media.mime_type or "video/mp4"
    if message.video_note:
        return message.video_note, "video_note.mp4", "video/mp4"
    if message.animation:
        media = message.animation
        return media, media.file_name or "animation.mp4", media.mime_type or ""
    if message.document:
        media = message.document
        return media, media.file_name or "upload.bin", media.mime_type or ""
    return None, "", ""


def looks_like_video(filename: str, mime: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS or mime.lower().startswith("video/")


def size_hint(services: BotServices) -> str:
    if services.big_files:
        return f"до {services.input_limit_mb} МБ"
    return f"до {services.input_limit_mb} МБ (лимит Bot API без TG_API_ID/TG_API_HASH)"


# --- access control --------------------------------------------------------

class AccessMiddleware(BaseMiddleware):
    def __init__(self, services: BotServices):
        self.services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        if self.services.claim_owner(user.id) and isinstance(event, Message):
            await event.answer(
                "👑 Ты — владелец этого бота. Другие люди доступа не получат: они увидят сообщение «частный бот»."
            )
        if self.services.user_allowed(user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(
                "⛔ Это частный бот.\n"
                f"Твой Telegram ID: <code>{user.id}</code> — владелец может добавить его в ALLOWED_USER_IDS."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


# --- commands and menu -----------------------------------------------------

async def cmd_start(message: Message, services: BotServices) -> None:
    services.jobs.drop(message.from_user.id)
    await message.answer(
        "Привет! Я <b>VIDEO RE-EDITOR</b> — бот для подготовки твоих видео к публикации.\n\n"
        f"Отправь ролик {size_hint(services)}. Лучше как <b>файл</b> — так Telegram его не сжимает. "
        "Я проверю параметры и предложу:\n"
        "• 🎲 <b>Уникализатор</b> — одна или несколько технически разных копий;\n"
        "• форматы Reels/Shorts 9:16, квадрат 1:1, мягкую цветокоррекцию.\n\n"
        "Важно: я не обещаю охваты и не могу гарантировать, как площадки оценят ролик. "
        "Используй видео и звук, на которые у тебя есть права.",
        reply_markup=main_keyboard(),
    )


async def cmd_help(message: Message, services: BotServices) -> None:
    mtproto = "включены (MTProto)" if services.mtproto_ready else (
        "Local Bot API Server" if services.settings.bot_api_mode == "local" else
        "выключены — нужны TG_API_ID и TG_API_HASH в .env"
    )
    await message.answer(
        "<b>Как пользоваться</b>\n"
        "1. Пришли видео (MP4, MOV, MKV, WebM, AVI…), лучше как файл.\n"
        "2. Выбери 🎲 Уникализатор или нужный формат.\n"
        "3. Получи готовый MP4. Исходник хранится "
        f"{services.settings.pending_ttl_minutes} мин — можно сделать ещё варианты без повторной загрузки.\n\n"
        f"Вход: {size_hint(services)}, до {services.settings.max_duration_seconds // 60} мин.\n"
        f"Выход: до {services.output_limit_mb} МБ.\n"
        f"Большие файлы: {mtproto}.\n"
        f"Доступ: {services.access_description}.\n\n"
        "/start — меню\n/cancel — остановить обработку или удалить исходник\n/id — твой Telegram ID"
    )


async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


async def cmd_cancel(message: Message, services: BotServices) -> None:
    user_id = message.from_user.id
    if services.jobs.request_cancel(user_id):
        await message.answer("⏹ Останавливаю обработку…")
        return
    if services.jobs.drop(user_id):
        await message.answer("Исходник удалён. Можешь прислать новое видео.", reply_markup=main_keyboard())
    else:
        await message.answer("Сейчас нечего отменять.", reply_markup=main_keyboard())


async def on_menu(callback: CallbackQuery, services: BotServices) -> None:
    action = (callback.data or "").split(":", 1)[-1]
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if action == "upload":
        await callback.message.answer(
            f"Пришли видео {size_hint(services)}. Лучше отправлять как файл — так Telegram не сжимает его."
        )
    elif action == "about":
        await callback.message.answer(
            "Обработка выполняется через FFmpeg на компьютере/сервере, где запущен бот — без платных сервисов. "
            f"Исходник удаляется через {services.settings.pending_ttl_minutes} мин без действий, по /cancel или "
            "при загрузке нового видео. Готовые файлы удаляются сразу после отправки. "
            "В истории остаются только технические параметры и контрольные суммы."
        )
    elif action == "history":
        rows = services.db.recent(callback.from_user.id)
        if not rows:
            await callback.message.answer("Пока нет завершённых обработок.")
            return
        lines = ["<b>Последние обработки</b>"]
        for row in rows:
            lines.append(
                f"• {html.escape(mode_title(row['preset']))} · {mb(row['output_bytes'])} · {row['created_at']} UTC"
            )
        await callback.message.answer("\n".join(lines))


async def on_other(message: Message, services: BotServices) -> None:
    await message.answer(f"Пришли видео {size_hint(services)} — как видео или как файл. /help — помощь.")


# --- receiving a video -----------------------------------------------------

async def download_source(
    bot: Bot, services: BotServices, message: Message, media: Any, destination: Path, status: Message,
) -> None:
    size = int(getattr(media, "file_size", 0) or 0)
    use_http = services.settings.bot_api_mode == "local" or size <= BOT_API_DOWNLOAD_LIMIT_MB * MB
    if use_http:
        try:
            await bot.download(media.file_id, destination=destination, timeout=DOWNLOAD_TIMEOUT)
            return
        except TelegramBadRequest as exc:
            if "too big" not in str(exc).lower() or not services.mtproto_ready:
                raise
            destination.unlink(missing_ok=True)
    if not services.mtproto_ready:
        raise MediaError(
            f"Файл больше {BOT_API_DOWNLOAD_LIMIT_MB} МБ, а MTProto не подключён. "
            "Добавь TG_API_ID и TG_API_HASH в .env и перезапусти бота."
        )
    progress = ProgressReporter(status, "📥 Скачиваю видео…")
    await services.mtproto.download(message.message_id, destination, progress=progress.on_bytes)
    await progress.done()


async def on_video(message: Message, bot: Bot, services: BotServices) -> None:
    settings = services.settings
    user_id = message.from_user.id
    media, filename, mime = extract_media(message)
    if media is None:
        return
    if not looks_like_video(filename, mime):
        await message.answer("Это не похоже на видео. Отправь MP4, MOV, M4V, MKV, WebM или AVI.")
        return

    limit_bytes = services.input_limit_mb * MB
    file_size = int(getattr(media, "file_size", 0) or 0)
    if file_size > limit_bytes:
        if services.big_files:
            hint = "Увеличь MAX_INPUT_MB только после проверки свободного места и ресурсов компьютера/сервера."
        else:
            hint = (
                f"Стандартный Telegram Bot API скачивает файлы до {BOT_API_DOWNLOAD_LIMIT_MB} МБ. "
                "Для больших файлов добавь в .env TG_API_ID и TG_API_HASH с my.telegram.org и перезапусти бота."
            )
        await message.answer(
            f"Файл весит {mb(file_size)}, а лимит текущей конфигурации — {services.input_limit_mb} МБ.\n\n{hint}"
        )
        return
    duration_hint = int(getattr(media, "duration", 0) or 0)
    if duration_hint > settings.max_duration_seconds:
        await message.answer(f"Видео длиннее разрешённых {settings.max_duration_seconds // 60} мин.")
        return
    if not services.allow_upload(user_id):
        await message.answer("Слишком много загрузок подряд. Подожди несколько минут и попробуй снова.")
        return

    job_dir = services.jobs.new_dir(user_id)
    suffix = Path(filename).suffix.lower()
    source = job_dir / f"source{suffix if suffix in VIDEO_EXTENSIONS else '.mp4'}"
    status = await message.answer("📥 Получил видео. Скачиваю и проверяю файл…")
    try:
        await download_source(bot, services, message, media, source, status)
        if not source.exists() or source.stat().st_size == 0:
            raise MediaError("Telegram вернул пустой файл")
        if source.stat().st_size > limit_bytes:
            raise MediaError(f"Файл после скачивания превышает лимит {services.input_limit_mb} МБ")
        info = await asyncio.to_thread(probe_video, source)
        if info.duration > settings.max_duration_seconds:
            raise MediaError(f"Видео длиннее разрешённых {settings.max_duration_seconds // 60} мин.")
        if info.width * info.height > MAX_PIXELS:
            raise MediaError("Разрешение выше 4K. Для экономии памяти принимаются видео до 4K.")
    except Exception as exc:
        remove_dir(job_dir)
        log.warning("Не удалось принять видео от %s: %s", user_id, exc, exc_info=not isinstance(exc, MediaError))
        await safe_edit(status, f"Не получилось принять видео: {friendly_error(exc)}")
        return

    services.jobs.set_pending(PendingUpload(
        user_id=user_id, chat_id=message.chat.id, message_id=message.message_id,
        job_dir=job_dir, source=source, filename=filename, info=info,
    ))
    await safe_edit(status, video_info_text(info), reply_markup=preset_keyboard())


# --- choosing what to do ---------------------------------------------------

@dataclass(frozen=True)
class JobSpec:
    kind: str  # "preset" or "unique"
    preset_key: str = ""
    options: UniqueOptions | None = None

    @property
    def copies(self) -> int:
        return self.options.copies if self.kind == "unique" and self.options else 1

    @property
    def history_key(self) -> str:
        if self.kind == "unique" and self.options:
            return f"unique:{self.options.intensity}"
        return self.preset_key


async def on_preset(callback: CallbackQuery, bot: Bot, services: BotServices) -> None:
    key = (callback.data or "").split(":", 1)[-1]
    if key not in PRESETS:
        await callback.answer()
        return
    await launch(callback, bot, services, JobSpec(kind="preset", preset_key=key))


async def on_unique(callback: CallbackQuery, bot: Bot, services: BotServices) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    user_id = callback.from_user.id
    pending = services.jobs.get(user_id)
    if pending is None:
        text = ("Сейчас идёт обработка — дождись результата." if services.jobs.is_active(user_id)
                else "Исходник не найден или устарел — пришли видео заново.")
        await callback.answer(text, show_alert=True)
        return

    max_copies = services.settings.max_unique_copies
    options = pending.unique
    if action == "go":
        await launch(callback, bot, services, JobSpec(kind="unique", options=options.normalized(max_copies)))
        return
    if action == "back":
        await callback.answer()
        await safe_edit(callback.message, video_info_text(pending.info), reply_markup=preset_keyboard())
        return
    if action == "int" and value in INTENSITIES:
        options.intensity = value
    elif action == "cnt" and value.isdigit() and int(value) in COPY_CHOICES:
        options.copies = min(int(value), max_copies)
    elif action == "mirror":
        options.mirror = not options.mirror
    elif action == "speed":
        options.speed = not options.speed
    elif action == "fmt":
        keys = list(FORMATS)
        options.fmt = keys[(keys.index(options.fmt) + 1) % len(keys)] if options.fmt in keys else keys[0]
    elif action != "open":
        await callback.answer()
        return
    await callback.answer()
    await safe_edit(callback.message, unique_panel_text(options, pending.info),
                    reply_markup=unique_keyboard(options, max_copies))


async def on_cancel(callback: CallbackQuery, services: BotServices) -> None:
    user_id = callback.from_user.id
    if not services.jobs.drop(user_id) and services.jobs.is_active(user_id):
        await callback.answer("Идёт обработка — нажми «⏹ Остановить» в сообщении с прогрессом.", show_alert=True)
        return
    await callback.answer("Отменено")
    await safe_edit(callback.message, "Выбор отменён, исходник удалён. Можешь прислать новое видео или открыть /start.")


async def on_stop(callback: CallbackQuery, services: BotServices) -> None:
    if services.jobs.request_cancel(callback.from_user.id):
        await callback.answer("Останавливаю…")
    else:
        await callback.answer("Сейчас ничего не обрабатывается.")


async def launch(callback: CallbackQuery, bot: Bot, services: BotServices, spec: JobSpec) -> None:
    user_id = callback.from_user.id
    # Everything up to spawning the task is synchronous, so a double tap cannot start two jobs.
    if services.jobs.is_active(user_id):
        await callback.answer("Уже обрабатываю предыдущий запрос — дождись результата.", show_alert=True)
        return
    if services.jobs.get(user_id) is None:
        await callback.answer("Исходник не найден или устарел — пришли видео заново.", show_alert=True)
        return
    if not services.allow_job(user_id):
        await callback.answer("Слишком много обработок подряд. Подожди несколько минут.", show_alert=True)
        return
    pending = services.jobs.take(user_id)
    assert pending is not None
    task = services.spawn(job_task(bot, services, pending, spec), name=f"job-{user_id}")
    services.jobs.start(user_id, task)
    await callback.answer("Запускаю…")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass


async def job_task(bot: Bot, services: BotServices, pending: PendingUpload, spec: JobSpec) -> None:
    try:
        await run_job(bot, services, pending, spec)
    finally:
        services.jobs.finish(pending.user_id)
        services.jobs.give_back(pending)


# --- running a job ---------------------------------------------------------

def build_caption(title: str, info: VideoInfo, size: int, input_hash: str, output_hash: str, details: str) -> str:
    # Telegram allows 1024 characters; only the free-form part is shortened so HTML tags stay intact.
    return (
        f"<b>{html.escape(title[:120])}</b>\n"
        f"{info.width}×{info.height} · {info.duration:.1f} сек. · {mb(size)}\n"
        f"{html.escape(details[:500])}\n\n"
        f"SHA-256 до: <code>{input_hash[:16]}</code>…\n"
        f"SHA-256 после: <code>{output_hash[:16]}</code>…\n"
        "Хеш — контрольная сумма файла; на охваты он не влияет."
    )


async def deliver(
    bot: Bot, services: BotServices, pending: PendingUpload, output: Path, info: VideoInfo,
    caption: str, thumb: Path | None, status: Message, file_name: str,
) -> None:
    size = output.stat().st_size
    if size <= BOT_API_SAFE_UPLOAD_MB * MB or services.settings.bot_api_mode == "local":
        async with ChatActionSender.upload_video(chat_id=pending.chat_id, bot=bot):
            await bot.send_video(
                chat_id=pending.chat_id,
                video=FSInputFile(output, filename=file_name),
                duration=max(1, round(info.duration)), width=info.width, height=info.height,
                thumbnail=FSInputFile(thumb) if thumb else None,
                caption=caption, supports_streaming=True, request_timeout=UPLOAD_TIMEOUT,
            )
        return
    if not services.mtproto_ready:
        raise MediaError(
            "Готовый файл больше 50 МБ, а MTProto не подключён. Добавь TG_API_ID и TG_API_HASH в .env."
        )
    progress = ProgressReporter(status, "📤 Отправляю готовое видео…", reply_markup=stop_keyboard())
    await services.mtproto.send_video(
        chat_id=pending.chat_id, message_id=pending.message_id, path=output, caption=caption,
        duration=info.duration, width=info.width, height=info.height, thumb=thumb, progress=progress.on_bytes,
    )
    await progress.done()


async def run_job(bot: Bot, services: BotServices, pending: PendingUpload, spec: JobSpec) -> None:
    settings = services.settings
    copies = spec.copies
    max_output_bytes = services.output_limit_mb * MB
    status = await bot.send_message(pending.chat_id, "⏳ Задача в очереди…", reply_markup=stop_keyboard())
    produced: list[Path] = []
    try:
        input_hash = await asyncio.to_thread(sha256_file, pending.source)
        for index in range(1, copies + 1):
            label = f" (копия {index} из {copies})" if copies > 1 else ""
            if spec.kind == "unique":
                output = pending.job_dir / f"unique_{index}.mp4"
                file_name = f"video_unique_{index}.mp4"
            else:
                output = pending.job_dir / f"{spec.preset_key}.mp4"
                file_name = "video_reedited.mp4"
            thumb_path = pending.job_dir / f"thumb_{index}.jpg"
            produced.extend([output, thumb_path])

            async with services.semaphore:
                progress = ProgressReporter(status, f"🎞 Обрабатываю видео{label}…", reply_markup=stop_keyboard(),
                                            min_interval=4.0)
                await safe_edit(status, f"🎞 Обрабатываю видео{label}…", reply_markup=stop_keyboard())
                if spec.kind == "unique" and spec.options:
                    params = generate_params(spec.options, pending.info)
                    result = await process_unique(
                        pending.source, output, params, info=pending.info,
                        timeout=settings.job_timeout_seconds, max_output_bytes=max_output_bytes,
                        on_progress=progress.update,
                    )
                    title = f"🎲 Уникализатор · {INTENSITIES.get(params.intensity, params.intensity)}{label}"
                    details = describe(params)
                else:
                    result = await process_video(
                        pending.source, output, spec.preset_key, info=pending.info,
                        timeout=settings.job_timeout_seconds, max_output_bytes=max_output_bytes,
                        on_progress=progress.update,
                    )
                    title = PRESETS[spec.preset_key].title
                    details = PRESETS[spec.preset_key].description
                await progress.done()

            thumb = await make_thumbnail(output, thumb_path, result.duration)
            output_hash = await asyncio.to_thread(sha256_file, output)
            output_size = output.stat().st_size
            caption = build_caption(title, result, output_size, input_hash, output_hash, details)
            await safe_edit(status, f"📤 Отправляю готовое видео{label}…", reply_markup=stop_keyboard())
            await deliver(bot, services, pending, output, result, caption, thumb, status, file_name)
            services.db.add_job(
                user_id=pending.user_id, file_name=pending.filename, preset=spec.history_key,
                input_bytes=pending.info.size_bytes, output_bytes=output_size,
                input_sha256=input_hash, output_sha256=output_hash,
            )
            output.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)

        await safe_delete(status)
        await bot.send_message(
            pending.chat_id,
            "✅ Готово! Можешь выбрать ещё вариант для этого же видео "
            f"(исходник хранится {settings.pending_ttl_minutes} мин) или прислать новое.",
            reply_markup=preset_keyboard(),
        )
    except asyncio.CancelledError:
        if not services.jobs.cancel_requested(pending.user_id):
            raise  # the bot is shutting down
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        await safe_edit(status, "⏹ Обработка остановлена.")
        await bot.send_message(pending.chat_id, "Можно выбрать другой вариант:", reply_markup=preset_keyboard())
    except Exception as exc:
        log.warning("Ошибка обработки для %s: %s", pending.user_id, exc,
                    exc_info=not isinstance(exc, (MediaError, MTProtoError)))
        await safe_edit(status, f"❌ Не удалось обработать видео: {friendly_error(exc)}")
        try:
            await bot.send_message(
                pending.chat_id, "Исходник сохранён — можно попробовать другой вариант:",
                reply_markup=preset_keyboard(),
            )
        except TelegramAPIError:
            pass
    finally:
        for path in produced:
            path.unlink(missing_ok=True)


def setup_router(dp: Dispatcher, services: BotServices, settings: Settings) -> Router:
    router = Router(name="video-re-editor")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    access = AccessMiddleware(services)
    router.message.middleware(access)
    router.callback_query.middleware(access)

    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_id, Command("id"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.callback_query.register(on_menu, F.data.startswith("menu:"))
    router.callback_query.register(on_preset, F.data.startswith("preset:"))
    router.callback_query.register(on_unique, F.data.startswith("uq:"))
    router.callback_query.register(on_cancel, F.data == "job:cancel")
    router.callback_query.register(on_stop, F.data == "job:stop")
    router.message.register(on_video, F.video | F.video_note | F.animation | F.document)
    router.message.register(on_other)

    dp.include_router(router)
    dp["services"] = services
    dp["settings"] = settings
    return router
