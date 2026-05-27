@echo off
title TheAgent0
setlocal

:: Single unified launcher.
:: 1. If dist\TheAgent0.exe exists, run it (packaged mode).
:: 2. Otherwise fall back to the venv Python dev mode.

cd /d "%~dp0"

if exist "dist\TheAgent0.exe" (
    echo [launch] Running packaged build: dist\TheAgent0.exe
    start "" "dist\TheAgent0.exe"
    exit /b 0
)

if not exist "venv\Scripts\python.exe" (
    echo [launch] venv not found. Run: python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

echo [launch] Running dev mode via venv
:: NOTE: invoke as a module (-m core.proxy), NOT as a script
:: (core\proxy.py). The script form puts core\ at the front of
:: sys.path, which makes core\secrets.py shadow the stdlib `secrets`
:: module — and that breaks huggingface_hub / diffusers (they
:: `from secrets import token_hex`). Module form puts the project
:: root on sys.path instead, so stdlib `secrets` resolves correctly.
start /b venv\Scripts\python -m core.proxy
timeout /t 3 >nul
start http://localhost:11435
echo.
echo TheAgent0 is running at http://localhost:11435
echo Close this window to stop the proxy.
pause
