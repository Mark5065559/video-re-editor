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

    def claim(self, user_id: int) -> bool:
        """The first caller becomes the owner. Synchronous, so two users cannot both win."""
        if self.owners or self.broken:
            return False
        self.owners.add(user_id)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({"owners": sorted(self.owners)}), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.exception("Не удалось сохранить владельца в %s (действует до перезапуска)", self.path)
        return True
