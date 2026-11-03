import pytest

from app.config import load_settings

TOKEN = "123456789:" + "A" * 35
ENV_KEYS = [
    "BOT_TOKEN", "BOT_API_MODE", "BOT_API_BASE_URL", "TG_API_ID", "TG_API_HASH", "ALLOWED_USER_IDS",
    "MAX_INPUT_MB", "MAX_OUTPUT_MB", "MAX_DURATION_SECONDS", "DATA_DIR", "MAX_PARALLEL_JOBS",
    "JOB_TIMEOUT_SECONDS", "USER_RATE_LIMIT_COUNT", "USER_RATE_LIMIT_WINDOW_SECONDS",
    "PENDING_TTL_MINUTES", "MAX_UNIQUE_COPIES", "ACCESS_MODE", "PORT", "WEBHOOK_BASE_URL",
    "RENDER_EXTERNAL_URL", "RUN_MODE", "KEEP_AWAKE", "CPU_PROFILE",
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CPU_PROFILE", "normal")  # do not depend on the test machine
    return monkeypatch


def test_missing_or_broken_token_is_reported(env):
    env.delenv("BOT_TOKEN")
    with pytest.raises(RuntimeError):
        load_settings(use_dotenv=False)
    env.setenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
    with pytest.raises(RuntimeError):
        load_settings(use_dotenv=False)
    env.setenv("BOT_TOKEN", "not-a-token")
    with pytest.raises(RuntimeError):
        load_settings(use_dotenv=False)


def test_official_mode_keeps_bot_api_limits(env):
    settings = load_settings(use_dotenv=False)
    assert (settings.max_input_mb, settings.max_output_mb) == (19, 47)
    assert not settings.mtproto_configured
    assert settings.bot_id == 123456789

    env.setenv("MAX_INPUT_MB", "60")
    env.setenv("MAX_OUTPUT_MB", "200")
    settings = load_settings(use_dotenv=False)
    assert (settings.max_input_mb, settings.max_output_mb) == (20, 47)


def test_api_credentials_enable_big_files(env):
    env.setenv("TG_API_ID", "12345")
    env.setenv("TG_API_HASH", "0123456789abcdef0123456789ABCDEF")
    settings = load_settings(use_dotenv=False)
    assert settings.mtproto_configured
    assert (settings.max_input_mb, settings.max_output_mb) == (300, 1000)
    assert settings.tg_api_hash == "0123456789abcdef0123456789abcdef"
    assert settings.mtproto_session_path.name == "mtproto_bot_123456789"

    env.setenv("MAX_INPUT_MB", "60")
    assert load_settings(use_dotenv=False).max_input_mb == 60
    env.setenv("MAX_INPUT_MB", "5000")
    with pytest.raises(ValueError):
        load_settings(use_dotenv=False)


@pytest.mark.parametrize("api_id,api_hash", [
    ("12345", ""), ("", "0123456789abcdef0123456789abcdef"), ("12a45", "0123456789abcdef0123456789abcdef"),
    ("12345", "short"),
])
def test_bad_api_credentials_are_rejected(env, api_id, api_hash):
    env.setenv("TG_API_ID", api_id)
    env.setenv("TG_API_HASH", api_hash)
    with pytest.raises(ValueError):
        load_settings(use_dotenv=False)


def test_allowed_users_and_local_mode(env):
    env.setenv("ALLOWED_USER_IDS", "111, 222;333 444")
    env.setenv("BOT_API_MODE", "local")
    env.setenv("BOT_API_BASE_URL", "http://127.0.0.1:8081/")
    settings = load_settings(use_dotenv=False)
    assert settings.allowed_user_ids == {111, 222, 333, 444} and settings.is_private_bot
    assert (settings.max_input_mb, settings.max_output_mb) == (500, 1800)
    assert settings.bot_api_base_url == "http://127.0.0.1:8081"

    env.setenv("ALLOWED_USER_IDS", "me")
    with pytest.raises(ValueError):
        load_settings(use_dotenv=False)


def test_access_mode(env):
    assert load_settings(use_dotenv=False).access_mode == "owner"
    assert load_settings(use_dotenv=False).is_private_bot
    env.setenv("ACCESS_MODE", "public")
    settings = load_settings(use_dotenv=False)
    assert settings.access_mode == "public" and not settings.is_private_bot
    env.setenv("ACCESS_MODE", "everyone")
    with pytest.raises(ValueError):
        load_settings(use_dotenv=False)


def test_cloud_settings(env):
    settings = load_settings(use_dotenv=False)
    assert (settings.run_mode, settings.port, settings.public_url, settings.keep_awake) == ("polling", 0, "", "busy")

    env.setenv("PORT", "10000")
    env.setenv("RENDER_EXTERNAL_URL", "https://video-re-editor.onrender.com/")
    env.setenv("CPU_PROFILE", "lite")
    settings = load_settings(use_dotenv=False)
    assert settings.run_mode == "webhook" and settings.port == 10000
    assert settings.public_url == "https://video-re-editor.onrender.com"
    assert settings.cpu_profile == "lite" and settings.job_timeout_seconds == 3600

    env.setenv("RUN_MODE", "polling")
    assert load_settings(use_dotenv=False).run_mode == "polling"
    env.setenv("RUN_MODE", "webhook")
    env.setenv("RENDER_EXTERNAL_URL", "http://insecure.example")
    with pytest.raises(ValueError):
        load_settings(use_dotenv=False)
