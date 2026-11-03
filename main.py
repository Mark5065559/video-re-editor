from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.config import load_settings
from app.db import HistoryDB
from app.handlers import BotServices, setup_router
from app.jobs import clean_temp_root
from app.mtproto import MTProtoFiles

log = logging.getLogger("video-re-editor")

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться и лимиты"),
    BotCommand(command="cancel", description="Остановить обработку или удалить исходник"),
    BotCommand(command="id", description="Показать мой Telegram ID"),
]


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except (RuntimeError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    removed = clean_temp_root(settings.temp_dir)
    if removed:
        log.info("Удалены временные папки от прошлого запуска: %s", removed)

    session = None
    if settings.bot_api_mode == "local":
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_base_url, is_local=True))
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    mtproto: MTProtoFiles | None = None
    services: BotServices | None = None
    cleanup_task: asyncio.Task | None = None
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram отклонил BOT_TOKEN. Проверь токен в .env или выпусти новый через @BotFather.")
            return 1
        except TelegramNetworkError as exc:
            log.error("Нет связи с %s: %s. Проверь интернет, VPN или прокси.", settings.bot_api_base_url, exc)
            return 1

        if settings.bot_api_mode == "official" and settings.mtproto_configured:
            mtproto = MTProtoFiles(
                api_id=settings.tg_api_id, api_hash=settings.tg_api_hash,
                bot_token=settings.bot_token, session_path=settings.mtproto_session_path,
            )
            if await mtproto.start():
                log.info("MTProto подключён: большие видео скачиваются и отправляются напрямую.")
            else:
                log.warning(
                    "MTProto не подключился (%s). Бот работает с лимитами Bot API: вход 20 МБ, выход 47 МБ.",
                    mtproto.error,
                )
        elif settings.mtproto_configured:
            log.info("BOT_API_MODE=local: большие файлы идут через Local Bot API Server, MTProto не нужен.")
        else:
            log.info("TG_API_ID/TG_API_HASH не заданы: лимиты Bot API — вход 20 МБ, выход 47 МБ.")

        db = HistoryDB(settings.db_path)
        services = BotServices(settings, db, mtproto)
        dp = Dispatcher(storage=MemoryStorage())
        setup_router(dp, services, settings)

        # A webhook left from another bot framework blocks getUpdates.
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except TelegramAPIError:
            log.warning("Не удалось установить список команд", exc_info=True)

        cleanup_task = asyncio.create_task(services.cleanup_loop())
        log.info(
            "Бот запущен: @%s | API mode=%s | вход до %s МБ, выход до %s МБ | доступ: %s",
            me.username, settings.bot_api_mode, services.input_limit_mb, services.output_limit_mb,
            services.access_description,
        )
        if settings.access_mode == "owner" and not settings.allowed_user_ids and not services.owner_store.owners:
            log.info("Владелец ещё не назначен: первый, кто напишет боту @%s, станет владельцем.", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
        if services is not None:
            await services.shutdown()
        if mtproto is not None:
            await mtproto.stop()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Бот остановлен.")
