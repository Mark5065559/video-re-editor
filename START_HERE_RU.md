# Начни отсюда

## Вариант 1 — бесплатно в облаке Render, без банковской карты (рекомендуется)

Ноутбук можно выключать, ботом пользуешься с телефона.

1. Войди на https://dashboard.render.com через GitHub. Если там уже круглосуточно работает другой бот, создай отдельное рабочее пространство (название слева вверху → **New Workspace**).
2. Открой https://render.com/deploy?repo=https://github.com/Mark5065559/video-re-editor/tree/claude/great-franklin-xpqj4a
3. Вставь три значения: `BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH` → **Apply**.
4. Через 5–10 минут напиши боту `/start`. Первый написавший становится владельцем.

Подробно — в начале `HOSTING_NOTES_RU.md`.

## Вариант 2 — мощный бесплатный сервер Oracle Cloud (нужна карта Visa/Mastercard)

Файл-установщик с уже вписанными ключами, пошагово — в `HOSTING_NOTES_RU.md`.

## Вариант 3 — на своём компьютере с Windows

1. Скачай проект с GitHub: **Code → Download ZIP** и распакуй.
2. Установи Python 3.13 (галочка **Add python.exe to PATH**) и FFmpeg (`winget install Gyan.FFmpeg` в PowerShell).
3. Скопируй `.env.example` в `.env` и заполни `BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`.
4. Запусти `run_windows.bat`.

Один токен — одна запущенная копия бота: не запускай его в двух местах сразу.

Полная инструкция, лимиты, настройки и диагностика — в `README_RU.md`.
