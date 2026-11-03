import asyncio
import json
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.db import HistoryDB
from app.handlers import BotServices, setup_router
from app.web import build_app, keep_awake, webhook_path, webhook_secret
from tests.test_bot_flow import TOKEN, FakeTelegram, make_settings


def test_webhook_path_and_secret_are_stable_and_safe():
    assert webhook_path(TOKEN) == webhook_path(TOKEN) and webhook_path(TOKEN).startswith("/tg/")
    assert TOKEN not in webhook_path(TOKEN)
    secret = webhook_secret(TOKEN)
    assert secret == webhook_secret(TOKEN) and len(secret) == 48 and secret.isalnum()
    assert webhook_path("1:" + "B" * 35) != webhook_path(TOKEN)


def test_health_and_webhook(tmp_path):
    async def scenario():
        session = FakeTelegram({})
        bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
        settings = make_settings(tmp_path)
        services = BotServices(settings, HistoryDB(settings.db_path))
        dp = Dispatcher()
        setup_router(dp, services, settings)
        client = TestClient(TestServer(build_app(dp, bot, services, webhook=True)))
        await client.start_server()
        try:
            response = await client.get("/health")
            data = await response.json()
            assert response.status == 200 and data["ok"] and data["busy"] is False

            update = {"update_id": 1, "message": {
                "message_id": 5, "date": int(time.time()), "chat": {"id": 777, "type": "private"},
                "from": {"id": 777, "is_bot": False, "first_name": "Owner"}, "text": "/id"}}
            denied = await client.post(webhook_path(TOKEN), json=update)
            assert denied.status == 401  # no secret header: not from Telegram
            accepted = await client.post(webhook_path(TOKEN), data=json.dumps(update),
                                         headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret(TOKEN),
                                                  "Content-Type": "application/json"})
            assert accepted.status == 200
            for _ in range(50):
                if any("777" in text for text in session.texts()):
                    break
                await asyncio.sleep(0.05)
            assert any("Твой Telegram ID" in text for text in session.texts())
        finally:
            await client.close()
            await bot.session.close()

    asyncio.run(scenario())


def test_keep_awake_pings_only_when_needed():
    class Services:
        def __init__(self):
            self.is_busy = False

        def busy(self):
            return self.is_busy

    async def scenario():
        hits = []

        async def health(_):
            hits.append(1)
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/health", health)
        server = TestServer(app)
        await server.start_server()
        url = str(server.make_url("")).rstrip("/")
        services = Services()
        task = asyncio.create_task(keep_awake(url, services, "busy", interval=0.05))
        await asyncio.sleep(0.3)
        assert hits == []  # idle: let the host put the bot to sleep
        services.is_busy = True
        await asyncio.sleep(0.3)
        assert len(hits) >= 2
        task.cancel()
        always = asyncio.create_task(keep_awake(url, Services(), "always", interval=0.05))
        before = len(hits)
        await asyncio.sleep(0.3)
        assert len(hits) > before
        always.cancel()
        await keep_awake(url, Services(), "off")  # returns immediately
        await server.close()

    asyncio.run(scenario())
