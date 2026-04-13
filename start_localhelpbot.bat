@echo off
title LocalHelpBot
setlocal

:: Single unified launcher.
:: 1. If dist\LocalHelpBot.exe exists, run it (packaged mode).
:: 2. Otherwise fall back to the venv Python dev mode.

cd /d "%~dp0"

if exist "dist\LocalHelpBot.exe" (
    echo [launch] Running packaged build: dist\LocalHelpBot.exe
    start "" "dist\LocalHelpBot.exe"
    exit /b 0
)

if not exist "venv\Scripts\python.exe" (
    echo [launch] venv not found. Run: python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

echo [launch] Running dev mode via venv
start /b venv\Scripts\python core\proxy.py
timeout /t 3 >nul
start http://localhost:11435
echo.
echo LocalHelpBot is running at http://localhost:11435
echo Close this window to stop the proxy.
pause
