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
    # owner: the first person who writes to the bot becomes its owner, others are refused;
    # public: anyone may use the bot (with rate limits). ALLOWED_USER_IDS overrides both.
    access_mode: str = "owner"
    # Cloud hosting (Render and similar): HTTP port for the health check / webhook and the public URL.
    port: int = 0
    public_url: str = ""
    run_mode: str = "polling"  # polling | webhook (resolved from RUN_MODE=auto)
    keep_awake: str = "busy"  # webhook mode: busy = stay awake only while working, always, off
    cpu_profile: str = "normal"  # normal | lite (resolved from CPU_PROFILE=auto)

    @property
    def mtproto_configured(self) -> bool:
        """api_id/api_hash are set: big files go through MTProto (Telethon)."""
        return self.tg_api_id is not None and bool(self.tg_api_hash)

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.split(":", 1)[0])

    @property
    def is_private_bot(self) -> bool:
        return bool(self.allowed_user_ids) or self.access_mode == "owner"

    @property
    def owners_path(self) -> Path:
        return self.data_dir / "owners.json"

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


def _cgroup_cpu_limit() -> float | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota_us > 0 and period_us > 0:
            return quota_us / period_us
    except (OSError, ValueError):
        pass
    return None


def _cgroup_memory_limit() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw != "max" and int(raw) < 1 << 50:
                return int(raw)
        except (OSError, ValueError):
            continue
    return None


def detect_weak_machine() -> bool:
    """Free cloud instances (e.g. Render: 0.1 CPU, 512 MB) need the light processing profile."""
    cpu = _cgroup_cpu_limit()
    memory = _cgroup_memory_limit()
    return (cpu is not None and cpu < 1.0) or (memory is not None and memory < 1536 * MB)


def _choice_env(name: str, default: str, allowed: set[str]) -> str:
    value = (os.getenv(name) or default).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} должен быть одним из: {', '.join(sorted(allowed))}")
    return value


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


# Values are often pasted with extra text ("BOT_TOKEN  123:AA...", quotes, all three lines in one
# field, swapped fields). Each value has a recognisable shape, so it is picked out wherever it is.
_TOKEN_SEARCH = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_HASH_SEARCH = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32}(?![0-9A-Za-z])")
_ID_SEARCH = re.compile(r"(?<![0-9A-Za-z:_-])\d{3,12}(?![0-9A-Za-z:_-])")


def _credential_texts() -> tuple[str, str, str]:
    return tuple((os.getenv(name) or "") for name in ("BOT_TOKEN", "TG_API_ID", "TG_API_HASH"))  # type: ignore[return-value]


def _find_token() -> str:
    token_raw, id_raw, hash_raw = _credential_texts()
    match = _TOKEN_SEARCH.search(token_raw) or _TOKEN_SEARCH.search(f"{id_raw} {hash_raw}")
    return match.group(0) if match else token_raw.strip().strip("'\"")


def _api_credentials() -> tuple[int | None, str | None]:
    token_raw, id_raw, hash_raw = _credential_texts()
    if not id_raw.strip() and not hash_raw.strip() and not _HASH_SEARCH.search(token_raw):
        return None, None
    everything = _TOKEN_SEARCH.sub(" ", f"{id_raw} {hash_raw} {token_raw}")
    hash_match = _HASH_SEARCH.search(_TOKEN_SEARCH.sub(" ", hash_raw)) or _HASH_SEARCH.search(everything)
    id_source = _HASH_SEARCH.sub(" ", _TOKEN_SEARCH.sub(" ", id_raw))
    id_match = _ID_SEARCH.search(id_source) or _ID_SEARCH.search(_HASH_SEARCH.sub(" ", everything))
    if not hash_match or not id_match:
        log.warning(
            "TG_API_ID/TG_API_HASH не распознаны (нужны число и 32 символа 0-9a-f с my.telegram.org): "
            "большие файлы отключены, бот работает с лимитом 20 МБ."
        )
        return None, None
    return int(id_match.group(0)), hash_match.group(0).lower()


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
    token = _find_token()
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

    access_mode = _choice_env("ACCESS_MODE", "owner", {"owner", "public"})

    port = _int_env("PORT", 0, minimum=0, maximum=65535)
    public_url = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    run_mode = _choice_env("RUN_MODE", "auto", {"auto", "polling", "webhook"})
    if run_mode == "auto":
        run_mode = "webhook" if public_url and port else "polling"
    if run_mode == "webhook":
        if not port or not public_url.startswith("https://"):
            raise ValueError("Для RUN_MODE=webhook нужны PORT и публичный адрес https:// (WEBHOOK_BASE_URL)")
    keep_awake = _choice_env("KEEP_AWAKE", "busy", {"busy", "always", "off"})
    if os.getenv("RENDER") and keep_awake == "busy":
        # A sleeping Render free instance answers Telegram's webhook with 502 and the message
        # is delayed for a long time, so on Render the bot must not fall asleep.
        log.info("Render: KEEP_AWAKE=busy заменён на always — спящий сервис отвечает Telegram ошибкой 502.")
        keep_awake = "always"
    cpu_profile = _choice_env("CPU_PROFILE", "auto", {"auto", "normal", "lite"})
    if cpu_profile == "auto":
        cpu_profile = "lite" if detect_weak_machine() else "normal"

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
        job_timeout_seconds=_int_env("JOB_TIMEOUT_SECONDS", 3600 if cpu_profile == "lite" else 900, minimum=30),
        user_rate_limit_count=_int_env("USER_RATE_LIMIT_COUNT", 3),
        user_rate_limit_window_seconds=_int_env("USER_RATE_LIMIT_WINDOW_SECONDS", 600),
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS") or ""),
        pending_ttl_minutes=_int_env("PENDING_TTL_MINUTES", 30),
        max_unique_copies=_int_env("MAX_UNIQUE_COPIES", 5, maximum=10),
        access_mode=access_mode,
        port=port,
        public_url=public_url,
        run_mode=run_mode,
        keep_awake=keep_awake,
        cpu_profile=cpu_profile,
    )
