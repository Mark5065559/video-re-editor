from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.sqlite3"


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом") from exc
    if value < minimum:
        raise ValueError(f"Переменная {name} должна быть не меньше {minimum}")
    return value


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    mode = os.getenv("BOT_API_MODE", "official").strip().lower()
    if mode not in {"official", "local"}:
        raise ValueError("BOT_API_MODE должен быть official или local")

    default_input = 19 if mode == "official" else 500
    default_output = 47 if mode == "official" else 1800
    return Settings(
        bot_token=token,
        bot_api_mode=mode,
        bot_api_base_url=os.getenv("BOT_API_BASE_URL", "https://api.telegram.org").rstrip("/"),
        max_input_mb=_int_env("MAX_INPUT_MB", default_input),
        max_output_mb=_int_env("MAX_OUTPUT_MB", default_output),
        max_duration_seconds=_int_env("MAX_DURATION_SECONDS", 600),
        data_dir=Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve(),
        max_parallel_jobs=_int_env("MAX_PARALLEL_JOBS", 1),
        job_timeout_seconds=_int_env("JOB_TIMEOUT_SECONDS", 900),
        user_rate_limit_count=_int_env("USER_RATE_LIMIT_COUNT", 3),
        user_rate_limit_window_seconds=_int_env("USER_RATE_LIMIT_WINDOW_SECONDS", 600),
    )
