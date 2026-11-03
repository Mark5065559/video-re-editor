import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetWebhook

import main


class FakeBot:
    token = "111111111:" + "A" * 35

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    async def set_webhook(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TelegramBadRequest(method=SetWebhook(url=url), message="Bad Request: bad webhook: Failed to resolve host")
        return True

    async def get_webhook_info(self):
        class Info:
            pending_update_count = 2
            last_error_message = None
        return Info()


class FakeDispatcher:
    def resolve_used_update_types(self):
        return ["message", "callback_query"]


def test_webhook_is_retried_until_telegram_accepts_it():
    bot = FakeBot(failures=2)
    assert asyncio.run(main.setup_webhook(bot, FakeDispatcher(), "https://x.onrender.com/tg/1", attempts=5, delay=0))
    assert bot.calls == 3


def test_webhook_gives_up_and_lets_the_bot_fall_back_to_polling():
    bot = FakeBot(failures=10)
    assert not asyncio.run(main.setup_webhook(bot, FakeDispatcher(), "https://x.onrender.com/tg/1", attempts=3, delay=0))
    assert bot.calls == 3
