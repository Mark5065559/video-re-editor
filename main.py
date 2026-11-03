from __future__ import annotations

import asyncio
import logging
import signal
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
from app.web import build_app, keep_awake, start_server, webhook_path, webhook_secret

log = logging.getLogger("video-re-editor")

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться и лимиты"),
    BotCommand(command="cancel", description="Остановить обработку или удалить исходник"),
    BotCommand(command="id", description="Показать мой Telegram ID"),
]


async def wait_for_stop_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    await stop.wait()


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
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

    webhook_mode = settings.run_mode == "webhook"
    log.info(
        "Режим: %s | обработка: %s%s", settings.run_mode, settings.cpu_profile,
        " (слабый сервер: 720p, быстрый кодек)" if settings.cpu_profile == "lite" else "",
    )
    session = None
    if settings.bot_api_mode == "local":
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_base_url, is_local=True))
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    mtproto: MTProtoFiles | None = None
    if settings.bot_api_mode == "official" and settings.mtproto_configured:
        mtproto = MTProtoFiles(
            api_id=settings.tg_api_id, api_hash=settings.tg_api_hash,
            bot_token=settings.bot_token, session_path=settings.mtproto_session_path,
        )
    services = BotServices(settings, HistoryDB(settings.db_path), mtproto, mtproto_lazy=webhook_mode)
    dp = Dispatcher(storage=MemoryStorage())
    setup_router(dp, services, settings)

    runner = None
    background: list[asyncio.Task] = []
    try:
        if settings.port:
            # Open the port first: cloud hosts wait for it before routing traffic.
            runner = await start_server(build_app(dp, bot, services, webhook=webhook_mode), settings.port)
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram отклонил BOT_TOKEN. Проверь токен или выпусти новый через @BotFather.")
            return 1
        except TelegramNetworkError as exc:
            log.error("Нет связи с %s: %s. Проверь интернет, VPN или прокси.", settings.bot_api_base_url, exc)
            return 1

        if mtproto is not None and not webhook_mode:
            if await mtproto.start():
                log.info("MTProto подключён: большие видео скачиваются и отправляются напрямую.")
            else:
                log.warning(
                    "MTProto не подключился (%s). Бот работает с лимитами Bot API: вход 20 МБ, выход 47 МБ.",
                    mtproto.error,
                )
        elif mtproto is not None:
            log.info("MTProto подключится, когда понадобится большой файл.")
        elif settings.mtproto_configured:
            log.info("BOT_API_MODE=local: большие файлы идут через Local Bot API Server, MTProto не нужен.")
        else:
            log.info("TG_API_ID/TG_API_HASH не заданы: лимиты Bot API — вход 20 МБ, выход 47 МБ.")

        await services.restore_owners(bot)
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except TelegramAPIError:
            log.warning("Не удалось установить список команд", exc_info=True)

        background.append(asyncio.create_task(services.cleanup_loop()))
        log.info(
            "Бот запущен: @%s | режим=%s | обработка=%s | вход до %s МБ, выход до %s МБ | доступ: %s",
            me.username, settings.run_mode, services.profile.name, services.input_limit_mb,
            services.output_limit_mb, services.access_description,
        )
        if services.remote_owners_enabled and not services.owner_store.owners:
            log.info("Владелец ещё не назначен: первый, кто напишет боту @%s, станет владельцем.", me.username)

        if webhook_mode:
            await bot.set_webhook(
                settings.public_url + webhook_path(bot.token), secret_token=webhook_secret(bot.token),
                allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False,
            )
            log.info("Вебхук: %s (засыпание без работы: %s)", settings.public_url, settings.keep_awake)
            background.append(asyncio.create_task(keep_awake(settings.public_url, services, settings.keep_awake)))
            await wait_for_stop_signal()
        else:
            # A webhook left from another bot framework blocks getUpdates.
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await services.shutdown()
        if runner is not None:
            await runner.cleanup()
        if mtproto is not None:
            await mtproto.stop()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Бот остановлен.")
