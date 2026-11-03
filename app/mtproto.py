"""Big files through MTProto.

The HTTP Bot API can download only 20 MB and upload 50 MB. With api_id/api_hash from
https://my.telegram.org the same bot also logs in through MTProto (Telethon) and can
transfer files up to 2000 MB. No separate server and no paid service is needed.

Only file transfer goes through this client; messages, buttons and updates stay on
aiogram. The client does not receive updates, so it never competes with polling.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# (bytes done, bytes total) -> may be async
BytesProgress = Callable[[int, int], "Awaitable[None] | None"]


class MTProtoError(RuntimeError):
    pass


class MTProtoFiles:
    def __init__(self, *, api_id: int, api_hash: str, bot_token: str, session_path: Path):
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self._session_path = session_path
        self._expected_bot_id = int(bot_token.split(":", 1)[0])
        self._client: Any = None
        self.available = False
        self.error: str | None = None

    async def start(self, timeout: float = 90) -> bool:
        try:
            from telethon import TelegramClient
        except ImportError:
            self.error = "не установлен пакет telethon (pip install -r requirements.txt)"
            return False
        try:
            import cryptg  # noqa: F401  # optional, makes transfers much faster
        except ImportError:
            log.warning("Пакет cryptg не установлен: большие файлы будут передаваться медленнее.")

        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(self._session_path), self._api_id, self._api_hash,
            receive_updates=False, device_model="video-re-editor", app_version="1.1",
            flood_sleep_threshold=120, request_retries=5, connection_retries=5,
        )
        try:
            await asyncio.wait_for(client.start(bot_token=self._bot_token), timeout)
            me = await client.get_me()
            if not getattr(me, "bot", False) or me.id != self._expected_bot_id:
                raise MTProtoError("сессия MTProto принадлежит другому аккаунту — удали файл mtproto_bot_*.session")
        except asyncio.TimeoutError:
            self.error = f"нет соединения с серверами Telegram (MTProto) за {timeout:.0f} сек."
            await self._safe_disconnect(client)
            return False
        except Exception as exc:  # wrong api_id/hash, network, flood wait...
            self.error = f"{type(exc).__name__}: {exc}"
            await self._safe_disconnect(client)
            return False
        self._client = client
        self.available = True
        self.error = None
        return True

    @staticmethod
    async def _safe_disconnect(client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception:
            pass

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                log.debug("MTProto disconnect failed", exc_info=True)
        self.available = False

    async def _message(self, message_id: int) -> Any:
        if not self.available or self._client is None:
            raise MTProtoError("MTProto не подключён")
        # messages.getMessages works for private chats without knowing the peer.
        messages = await self._client.get_messages(None, ids=[message_id])
        message = messages[0] if messages else None
        if message is None or getattr(message, "media", None) is None:
            raise MTProtoError(
                "Telegram не нашёл сообщение с видео. Пришли видео ещё раз и не удаляй его до конца обработки."
            )
        return message

    async def download(self, message_id: int, destination: Path, progress: BytesProgress | None = None) -> None:
        message = await self._message(message_id)
        result = await self._client.download_media(message, file=str(destination), progress_callback=progress)
        if not result or not destination.exists():
            raise MTProtoError("Не удалось скачать файл через MTProto")

    async def send_video(
        self, *, chat_id: int, message_id: int, path: Path, caption: str, duration: float,
        width: int, height: int, thumb: Path | None = None, progress: BytesProgress | None = None,
    ) -> None:
        from telethon.tl.types import DocumentAttributeVideo

        try:
            # Fetching the user's message also gives Telethon the access hash of the chat.
            peer = await (await self._message(message_id)).get_input_chat()
        except MTProtoError:
            peer = await self._client.get_input_entity(chat_id)
        await self._client.send_file(
            peer, str(path), caption=caption, parse_mode="html", supports_streaming=True,
            attributes=[DocumentAttributeVideo(
                duration=round(duration, 3), w=width, h=height, supports_streaming=True,
            )],
            thumb=str(thumb) if thumb else None, progress_callback=progress,
        )
