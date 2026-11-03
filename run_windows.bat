@echo off
cd /d "%~dp0"
if not exist .env (
  echo [ОШИБКА] Файл .env не найден.
  echo Скопируй .env.example в .env и вставь токен от @BotFather.
  pause
  exit /b 1
)
python --version >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Python не найден. Установи Python 3.12 или новее и включи Add Python to PATH.
  pause
  exit /b 1
)
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
pause
