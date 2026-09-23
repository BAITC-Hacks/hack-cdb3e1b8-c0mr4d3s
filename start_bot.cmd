@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Follow installation steps in README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -B -X utf8 -m scripts.bot %*
if errorlevel 1 (
    echo.
    echo Fix the error shown above, then run start_bot.cmd again.
    pause
    exit /b 1
)
