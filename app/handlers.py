from __future__ import annotations

import asyncio
import html
import logging
import shutil
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .config import Settings
from .db import HistoryDB
from .keyboards import main_keyboard, preset_keyboard
from .media import PRESETS, probe_video, process_video, sha256_file

log = logging.getLogger(__name__)
router = Router()


class UploadState(StatesGroup):
    choosing_preset = State()


class BotServices:
    def __init__(self, settings: Settings, db: HistoryDB):
        self.settings = settings
        self.db = db
        self.semaphore = asyncio.Semaphore(settings.max_parallel_jobs)
        self.rate: dict[int, deque[float]] = defaultdict(deque)
        self.user_dirs: dict[int, Path] = {}

    def allowed_now(self, user_id: int) -> bool:
        now = time.monotonic()
        bucket = self.rate[user_id]
        while bucket and now - bucket[0] > self.settings.user_rate_limit_window_seconds:
            bucket.popleft()
        if len(bucket) >= self.settings.user_rate_limit_count:
            return False
        bucket.append(now)
        return True

    def new_job_dir(self, user_id: int) -> Path:
        old = self.user_dirs.get(user_id)
        if old:
            shutil.rmtree(old, ignore_errors=True)
        path = self.settings.temp_dir / f"{user_id}_{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=True)
        self.user_dirs[user_id] = path
        return path

    def clear_user_dir(self, user_id: int) -> None:
        path = self.user_dirs.pop(user_id, None)
        if path:
            shutil.rmtree(path, ignore_errors=True)


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я <b>VIDEO RE-EDITOR</b> — бот для подготовки твоих видео к публикации.\n\n"
        "Отправь ролик как видео или файл. Я проверю его параметры и предложу варианты монтажа.\n\n"
        "Важно: я не обещаю охваты и не обхожу системы распознавания платформ. "
        "Используй видео и звук, на которые у тебя есть права.",
        reply_markup=main_keyboard(),
    )


async def on_menu(callback: CallbackQuery, state: FSMContext, services: BotServices) -> None:
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "upload":
        services.clear_user_dir(callback.from_user.id)
        await state.clear()
        await callback.message.answer("Пришли видео. Для стандартного Telegram Bot API предел скачивания через getFile — 20 МБ.")
    elif action == "about":
        await callback.message.answer(
            "Обработка выполняется через FFmpeg. Временные видео удаляются после отправки результата. "
            "В истории остаются только технические параметры и контрольные суммы файлов."
        )
    elif action == "history":
        rows = services.db.recent(callback.from_user.id)
        if not rows:
            await callback.message.answer("Пока нет завершённых обработок.")
        else:
            lines = ["<b>Последние обработки</b>"]
            for row in rows:
                mb = row["output_bytes"] / (1024 * 1024)
                lines.append(f"• {row['preset']} · {mb:.1f} МБ · {row['created_at']}")
            await callback.message.answer("\n".join(lines))


async def on_video(message: Message, state: FSMContext, services: BotServices, settings: Settings, bot) -> None:
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return
    if not services.allowed_now(user_id):
        await message.answer("Слишком много загрузок подряд. Подожди несколько минут и попробуй снова.")
        return

    await state.clear()
    services.clear_user_dir(user_id)
    media = message.video or message.document
    if media is None:
        return
    filename = getattr(media, "file_name", None) or ("video.mp4" if message.video else "upload.mp4")
    suffix = Path(filename).suffix.lower()
    allowed_ext = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
    mime = getattr(media, "mime_type", None) or ""
    if suffix not in allowed_ext and not mime.startswith("video/"):
        await message.answer("Это не похоже на видео. Отправь MP4, MOV, M4V, MKV или WebM.")
        return

    max_in = settings.max_input_mb * 1024 * 1024
    file_size = getattr(media, "file_size", None)
    if file_size and file_size > max_in:
        await message.answer(
            f"Файл весит {file_size / 1024 / 1024:.1f} МБ, а лимит текущей конфигурации — {settings.max_input_mb} МБ.\n\n"
            + ("Стандартный Telegram Bot API скачивает через getFile файлы до 20 МБ. Для больших файлов нужен собственный Local Bot API Server."
               if settings.bot_api_mode == "official" else "Увеличь MAX_INPUT_MB только после проверки свободного места и ресурсов сервера.")
        )
        return

    job_dir = services.new_job_dir(user_id)
    safe_suffix = suffix if suffix in allowed_ext else ".mp4"
    source = job_dir / f"source{safe_suffix}"
    status = await message.answer("📥 Получил видео. Скачиваю и проверяю файл…")
    try:
        telegram_file = await bot.get_file(media.file_id)
        await bot.download_file(telegram_file.file_path, destination=source)
        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Telegram вернул пустой файл")
        if source.stat().st_size > max_in:
            raise ValueError(f"Файл после скачивания превышает лимит {settings.max_input_mb} МБ")
        info = await asyncio.to_thread(probe_video, source)
        if info.duration > settings.max_duration_seconds:
            raise ValueError(f"Видео длиннее разрешённых {settings.max_duration_seconds // 60} минут")
        if info.width * info.height > 3840 * 2160:
            raise ValueError("Разрешение выше 4K. Для экономии памяти принимаются видео до 4K.")
        await state.set_state(UploadState.choosing_preset)
        await state.update_data(
            user_dir=str(job_dir), source=str(source), filename=filename,
            has_audio=info.has_audio, input_bytes=info.size_bytes,
        )
        await status.edit_text(
            f"<b>Видео проверено</b>\n"
            f"Разрешение: {info.width}×{info.height}\n"
            f"Длительность: {info.duration:.1f} сек.\n"
            f"Размер: {info.size_bytes / 1024 / 1024:.1f} МБ\n"
            f"Звук: {'есть' if info.has_audio else 'нет'}\n\n"
            "Выбери вариант подготовки:",
            reply_markup=preset_keyboard(),
        )
    except Exception as exc:
        services.clear_user_dir(user_id)
        log.exception("Не удалось принять видео от %s", user_id)
        await status.edit_text(f"Не получилось принять видео: {html.escape(str(exc)[:700])}")


async def on_preset(callback: CallbackQuery, state: FSMContext, services: BotServices, settings: Settings, bot) -> None:
    await callback.answer()
    preset_key = callback.data.split(":", 1)[1]
    if preset_key not in PRESETS:
        return
    data = await state.get_data()
    if not data.get("source"):
        await callback.message.answer("Не нашёл исходный файл. Отправь видео заново.")
        await state.clear()
        return

    user_id = callback.from_user.id
    source = Path(data["source"])
    job_dir = Path(data["user_dir"])
    output = job_dir / "processed.mp4"
    preset = PRESETS[preset_key]
    await state.clear()
    status = await callback.message.answer("⚙️ Ставлю задачу в очередь…")
    try:
        async with services.semaphore:
            await status.edit_text("🎞 Обрабатываю видео. Это может занять некоторое время…")
            await bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
            info = await asyncio.wait_for(
                asyncio.to_thread(
                    process_video, source, output, preset_key,
                    has_audio=bool(data.get("has_audio")),
                    timeout=settings.job_timeout_seconds,
                    max_output_bytes=settings.max_output_mb * 1024 * 1024,
                ),
                timeout=settings.job_timeout_seconds + 30,
            )

        input_hash = await asyncio.to_thread(sha256_file, source)
        output_hash = await asyncio.to_thread(sha256_file, output)
        output_size = output.stat().st_size
        caption = (
            f"<b>{preset.title}</b>\n"
            f"{info.width}×{info.height} · {info.duration:.1f} сек. · {output_size / 1024 / 1024:.1f} МБ\n\n"
            f"SHA-256 до: <code>{input_hash[:16]}</code>…\n"
            f"SHA-256 после: <code>{output_hash[:16]}</code>…\n\n"
            "Хеш — только контрольная сумма файла; он не доказывает оригинальность и не влияет на охваты."
        )
        await status.edit_text("✅ Готово! Отправляю обработанный ролик…")
        await bot.send_video(
            chat_id=callback.message.chat.id,
            video=FSInputFile(output, filename="video_reedited.mp4"),
            caption=caption,
            supports_streaming=True,
        )
        services.db.add_job(
            user_id=user_id, file_name=data.get("filename", "video"), preset=preset_key,
            input_bytes=int(data.get("input_bytes", 0)), output_bytes=output_size,
            input_sha256=input_hash, output_sha256=output_hash,
        )
        await status.delete()
        await callback.message.answer("Готово. Можешь прислать следующий ролик.", reply_markup=main_keyboard())
    except asyncio.TimeoutError:
        await status.edit_text("Обработка заняла слишком много времени и была остановлена. Попробуй более короткий ролик.")
    except Exception as exc:
        log.exception("Ошибка обработки для %s", user_id)
        await status.edit_text(f"Не удалось обработать видео: {html.escape(str(exc)[:700])}")
    finally:
        services.clear_user_dir(user_id)


async def on_cancel(callback: CallbackQuery, state: FSMContext, services: BotServices) -> None:
    await callback.answer("Отменено")
    services.clear_user_dir(callback.from_user.id)
    await state.clear()
    await callback.message.edit_text("Выбор отменён. Можешь начать заново через меню /start.")


def setup_router(dp, services: BotServices, settings: Settings) -> None:
    router.message.register(cmd_start, F.text == "/start")
    router.callback_query.register(on_menu, F.data.startswith("menu:"))
    router.callback_query.register(on_preset, F.data.startswith("preset:"))
    router.callback_query.register(on_cancel, F.data == "job:cancel")
    router.message.register(on_video, F.video | F.document)
    dp.include_router(router)
    dp["services"] = services
    dp["settings"] = settings
