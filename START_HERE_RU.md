# Начни отсюда

## Вариант 1 — бот работает круглосуточно на бесплатном сервере (рекомендуется)

Ноутбук можно выключать, ботом пользуешься с телефона.

1. Сделай репозиторий на GitHub публичным: https://github.com/Mark5065559/video-re-editor/settings → внизу **Danger Zone** → **Change visibility** → **Public**.
2. Зарегистрируйся в Oracle Cloud (бесплатно, нужна карта для проверки) и создай сервер **Canonical Ubuntu 24.04**, **VM.Standard.A1.Flex**. В разделе **Advanced options → Management → cloud-init script** выбери файл `video-re-editor-oracle-install.sh` — в нём уже вписаны токен и ключи.
3. Через 10–15 минут напиши боту `/start` в Telegram. Первый написавший становится владельцем.

Подробно, с названиями всех кнопок, — в `HOSTING_NOTES_RU.md`.

## Вариант 2 — бот работает на своём компьютере с Windows

1. Скачай проект с GitHub: **Code → Download ZIP** и распакуй.
2. Установи Python 3.13 (галочка **Add python.exe to PATH**) и FFmpeg (`winget install Gyan.FFmpeg` в PowerShell).
3. Скопируй `.env.example` в `.env` и заполни `BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH` (с https://my.telegram.org).
4. Запусти `run_windows.bat`. Дождись строк «Бот запущен» и «MTProto подключён».
5. Открой бота в Telegram, нажми `/start` и отправь видео как файл.

Не запускай бота на компьютере, пока он работает на сервере: один токен — одна копия.

Полная инструкция, лимиты, настройки и диагностика — в `README_RU.md`.
