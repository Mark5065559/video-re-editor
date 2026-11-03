"""Who may use the bot when ALLOWED_USER_IDS is not set: the first person becomes the owner."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class OwnerStore:
    def __init__(self, path: Path):
        self.path = path
        self.owners: set[int] = set()
        self.broken = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.owners = {int(value) for value in data.get("owners", [])}
        except (OSError, ValueError, TypeError, AttributeError):
            # Never hand the bot to a stranger because of a damaged file.
            self.broken = True
            log.error(
                "Файл владельцев %s повреждён: новые владельцы не назначаются. Удали файл, чтобы назначить заново.",
                self.path,
            )

    def adopt(self, owners: set[int]) -> None:
        """Owners restored from Telegram after the local disk was wiped."""
        if self.owners or not owners:
            return
        self.owners = set(owners)
        self._write()

    def claim(self, user_id: int) -> bool:
        """The first caller becomes the owner. Synchronous, so two users cannot both win."""
        if self.owners or self.broken:
            return False
        self.owners.add(user_id)
        self._write()
        return True

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({"owners": sorted(self.owners)}), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.exception("Не удалось сохранить владельца в %s (действует до перезапуска)", self.path)


# On free cloud hosting the disk is wiped at every restart, so the owner is also kept on
# Telegram's side: as a bot command list for a scope and language nobody uses here (Zulu
# group administrators; the bot only works in private chats). Nobody sees it in practice.
_REMOTE_COMMAND = "vreowner"
_REMOTE_LANGUAGE = "zu"


async def load_remote_owners(bot) -> set[int]:
    from aiogram.types import BotCommandScopeAllChatAdministrators

    commands = await bot.get_my_commands(scope=BotCommandScopeAllChatAdministrators(), language_code=_REMOTE_LANGUAGE)
    for command in commands:
        if command.command == _REMOTE_COMMAND:
            return {int(part) for part in command.description.split() if part.isdigit()}
    return set()


async def save_remote_owners(bot, owners: set[int]) -> None:
    from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators

    description = " ".join(str(owner) for owner in sorted(owners)) or "-"
    await bot.set_my_commands(
        [BotCommand(command=_REMOTE_COMMAND, description=description)],
        scope=BotCommandScopeAllChatAdministrators(), language_code=_REMOTE_LANGUAGE,
    )
