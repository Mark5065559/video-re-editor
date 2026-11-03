"""HTTP side for cloud hosting (Render and similar).

- /health answers the host's health checks;
- the Telegram webhook wakes a sleeping free instance when a message arrives;
- while the bot is working it calls its own public address so the host does not put it
  to sleep in the middle of a job (Render sleeps after 15 minutes without requests).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

log = logging.getLogger(__name__)
STARTED = time.time()


def webhook_path(token: str) -> str:
    return "/tg/" + hashlib.sha256(f"path:{token}".encode()).hexdigest()[:32]


def webhook_secret(token: str) -> str:
    # Telegram sends it back in X-Telegram-Bot-Api-Secret-Token; only [A-Za-z0-9_-] allowed.
    return hashlib.sha256(f"secret:{token}".encode()).hexdigest()[:48]


def build_app(dp: Dispatcher, bot: Bot, services, *, webhook: bool) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "uptime": int(time.time() - STARTED),
            "busy": services.busy(),
            "jobs": services.jobs.active_count(),
            "profile": services.profile.name,
        })

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    if webhook:
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=webhook_secret(bot.token)).register(
            app, path=webhook_path(bot.token),
        )
    return app


async def start_server(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP-сервер слушает порт %s", port)
    return runner


async def keep_awake(url: str, services, mode: str, interval: float = 300) -> None:
    """mode=busy: ping only while something is going on; mode=always: ping all the time."""
    if mode == "off" or not url:
        return
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        while True:
            await asyncio.sleep(interval)
            if mode != "always" and not services.busy():
                continue
            try:
                async with session.get(f"{url}/health") as response:
                    await response.read()
            except (ClientError, asyncio.TimeoutError) as exc:
                log.info("Не удалось обратиться к %s: %s", url, exc)
