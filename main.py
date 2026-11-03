from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import load_settings
from app.db import HistoryDB
from app.handlers import BotServices, setup_router


async def main() -> None:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)

    # Remove abandoned job folders from a previous unclean shutdown (older than 12 hours).
    cutoff = time.time() - 12 * 60 * 60
    for folder in settings.temp_dir.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            logging.exception("Не удалось очистить временную папку %s", folder)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    session = None
    if settings.bot_api_mode == "local":
        api = TelegramAPIServer.from_base(settings.bot_api_base_url, is_local=True)
        session = AiohttpSession(api=api)

    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    db = HistoryDB(settings.db_path)
    services = BotServices(settings, db)
    dp = Dispatcher(storage=MemoryStorage())
    setup_router(dp, services, settings)

    me = await bot.get_me()
    logging.info("Бот запущен: @%s | API mode=%s", me.username, settings.bot_api_mode)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
