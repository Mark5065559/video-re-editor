# Начни отсюда

1. Скачай проект с GitHub: **Code → Download ZIP** и распакуй.
2. Установи Python 3.13 (галочка **Add python.exe to PATH**) и FFmpeg (`winget install Gyan.FFmpeg` в PowerShell).
3. Скопируй `.env.example` в `.env` и заполни в Блокноте:
   - `BOT_TOKEN=` — токен от `@BotFather`;
   - `TG_API_ID=` и `TG_API_HASH=` — с https://my.telegram.org → API development tools. Без них бот принимает видео только до 20 МБ, с ними — до 300 МБ (можно поднять до 2000);
   - `ALLOWED_USER_IDS=` — свой Telegram ID (бот покажет его по команде `/id`), чтобы ботом не пользовались посторонние.
4. Не добавляй `.env` и папку `data` в GitHub — они уже в `.gitignore`.
5. Запусти `run_windows.bat`. Дождись строки «Бот запущен» и «MTProto подключён».
6. Открой бота в Telegram, нажми `/start` и отправь видео как файл. Выбери **🎲 Уникализатор** или нужный формат.

Бот работает, пока открыто окно и включён компьютер. Бесплатный круглосуточный вариант — Oracle Cloud Always Free, инструкция в `HOSTING_NOTES_RU.md`.

Полная инструкция, лимиты, настройки и диагностика — в `README_RU.md`.
