@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title VIDEO RE-EDITOR

if not exist ".env" goto :no_env

rem Prefer tested Python versions: newer releases may lack ready-made packages.
set "PY="
for %%V in (3.13 3.12 3.14 3.11 3.10) do (
  if not defined PY (
    py -%%V --version >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY goto :no_python

where ffmpeg >nul 2>&1 || goto :no_ffmpeg
where ffprobe >nul 2>&1 || goto :no_ffmpeg

if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение .venv ...
  %PY% -m venv .venv || goto :venv_failed
)

echo Проверяю зависимости...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :pip_failed

echo Запускаю бота. Для остановки нажми Ctrl+C.
".venv\Scripts\python.exe" main.py
pause
exit /b 0

:no_env
echo [ОШИБКА] Файл .env не найден.
echo Скопируй .env.example в .env и вставь токен от @BotFather.
pause
exit /b 1

:no_python
echo [ОШИБКА] Python не найден. Установи Python 3.13 или 3.12 с python.org
echo и включи галочку "Add python.exe to PATH".
pause
exit /b 1

:no_ffmpeg
echo [ОШИБКА] FFmpeg не найден. Установи его командой: winget install Gyan.FFmpeg
echo Затем закрой это окно и запусти run_windows.bat заново.
pause
exit /b 1

:venv_failed
echo [ОШИБКА] Не удалось создать виртуальное окружение .venv
pause
exit /b 1

:pip_failed
echo [ОШИБКА] Не удалось установить зависимости. Проверь интернет.
echo Подходят Python 3.10-3.14. Если стоит более новый, установи 3.13 и удали папку .venv
pause
exit /b 1
