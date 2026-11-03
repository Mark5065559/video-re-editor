from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

MB = 1024 * 1024

# Hard limits of the official HTTP Bot API: getFile downloads up to 20 MB,
# sendVideo uploads up to 50 MB. https://core.telegram.org/bots/api#getfile
BOT_API_DOWNLOAD_LIMIT_MB = 20
BOT_API_UPLOAD_LIMIT_MB = 50
# Files above this size are sent through MTProto when it is available.
BOT_API_SAFE_UPLOAD_MB = 49
# Bots can transfer files up to 2000 MB through MTProto or a Local Bot API Server.
MTPROTO_LIMIT_MB = 2000

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_api_mode: str
    bot_api_base_url: str
    max_input_mb: int
    max_output_mb: int
    max_duration_seconds: int
    data_dir: Path
    max_parallel_jobs: int
    job_timeout_seconds: int
    user_rate_limit_count: int
    user_rate_limit_window_seconds: int
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    allowed_user_ids: frozenset[int] = frozenset()
    pending_ttl_minutes: int = 30
    max_unique_copies: int = 5

    @property
    def mtproto_configured(self) -> bool:
        """api_id/api_hash are set: big files go through MTProto (Telethon)."""
        return self.tg_api_id is not None and bool(self.tg_api_hash)

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.split(":", 1)[0])

    @property
    def is_private_bot(self) -> bool:
        return bool(self.allowed_user_ids)

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @property
    def mtproto_session_path(self) -> Path:
        # Telethon appends ".session". The bot id in the name keeps sessions of
        # different bots apart if the token is ever changed.
        return self.data_dir / f"mtproto_bot_{self.bot_id}"


def _int_env(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом") from exc
    if value < minimum:
        raise ValueError(f"Переменная {name} должна быть не меньше {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Переменная {name} должна быть не больше {maximum}")
    return value


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in re.split(r"[\s,;]+", raw.strip()):
        if not part:
            continue
        if not re.fullmatch(r"-?\d+", part):
            raise ValueError(
                f"ALLOWED_USER_IDS: «{part}» — не число. Укажи числовые ID через запятую (узнать свой — команда /id)."
            )
        ids.add(int(part))
    return frozenset(ids)


def _api_credentials() -> tuple[int | None, str | None]:
    raw_id = (os.getenv("TG_API_ID") or "").strip()
    raw_hash = (os.getenv("TG_API_HASH") or "").strip()
    if not raw_id and not raw_hash:
        return None, None
    if not raw_id or not raw_hash:
        raise ValueError("Укажи оба значения TG_API_ID и TG_API_HASH (или оставь оба пустыми).")
    if not raw_id.isdigit():
        raise ValueError("TG_API_ID должен состоять только из цифр (см. my.telegram.org).")
    if not _API_HASH_RE.fullmatch(raw_hash):
        raise ValueError("TG_API_HASH должен быть строкой из 32 шестнадцатеричных символов (см. my.telegram.org).")
    return int(raw_id), raw_hash.lower()


def load_env_file() -> Path | None:
    """Reads .env from the current folder or the project folder. Real environment variables win."""
    for folder in dict.fromkeys((Path.cwd(), PROJECT_ROOT)):
        candidate = folder / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def _missing_token_hint() -> str:
    for folder in dict.fromkeys((Path.cwd(), PROJECT_ROOT)):
        for wrong in (".env.txt", "env", "env.txt", ".env.example.txt"):
            if (folder / wrong).is_file():
                return f" Найден файл «{wrong}» — переименуй его ровно в «.env» (без .txt)."
    return ""


def load_settings(*, use_dotenv: bool = True) -> Settings:
    if use_dotenv:
        load_env_file()
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
            + (_missing_token_hint() if use_dotenv else "")
        )
    if not _TOKEN_RE.fullmatch(token):
        raise RuntimeError(
            "BOT_TOKEN выглядит неправильно. Он должен быть вида 123456789:AA... — скопируй его из @BotFather целиком."
        )

    mode = (os.getenv("BOT_API_MODE") or "official").strip().lower()
    if mode not in {"official", "local"}:
        raise ValueError("BOT_API_MODE должен быть official или local")

    api_id, api_hash = _api_credentials()
    if mode == "local":
        default_input, default_output = 500, 1800
    elif api_id is not None:
        default_input, default_output = 300, 1000
    else:
        default_input, default_output = 19, 47

    max_input_mb = _int_env("MAX_INPUT_MB", default_input, maximum=MTPROTO_LIMIT_MB)
    max_output_mb = _int_env("MAX_OUTPUT_MB", default_output, maximum=MTPROTO_LIMIT_MB)
    if mode == "official" and api_id is None:
        if max_input_mb > BOT_API_DOWNLOAD_LIMIT_MB:
            log.warning(
                "MAX_INPUT_MB=%s, но без TG_API_ID/TG_API_HASH Bot API скачивает максимум %s МБ — использую %s.",
                max_input_mb, BOT_API_DOWNLOAD_LIMIT_MB, BOT_API_DOWNLOAD_LIMIT_MB,
            )
            max_input_mb = BOT_API_DOWNLOAD_LIMIT_MB
        if max_output_mb > BOT_API_UPLOAD_LIMIT_MB:
            log.warning(
                "MAX_OUTPUT_MB=%s, но без TG_API_ID/TG_API_HASH Bot API отправляет максимум %s МБ — использую 47.",
                max_output_mb, BOT_API_UPLOAD_LIMIT_MB,
            )
            max_output_mb = 47

    return Settings(
        bot_token=token,
        bot_api_mode=mode,
        bot_api_base_url=(os.getenv("BOT_API_BASE_URL") or "https://api.telegram.org").strip().rstrip("/"),
        max_input_mb=max_input_mb,
        max_output_mb=max_output_mb,
        max_duration_seconds=_int_env("MAX_DURATION_SECONDS", 600),
        data_dir=Path(os.getenv("DATA_DIR") or "./data").expanduser().resolve(),
        max_parallel_jobs=_int_env("MAX_PARALLEL_JOBS", 1),
        job_timeout_seconds=_int_env("JOB_TIMEOUT_SECONDS", 900, minimum=30),
        user_rate_limit_count=_int_env("USER_RATE_LIMIT_COUNT", 3),
        user_rate_limit_window_seconds=_int_env("USER_RATE_LIMIT_WINDOW_SECONDS", 600),
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS") or ""),
        pending_ttl_minutes=_int_env("PENDING_TTL_MINUTES", 30),
        max_unique_copies=_int_env("MAX_UNIQUE_COPIES", 5, maximum=10),
    )
