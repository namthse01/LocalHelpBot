@echo off
title LocalHelpBot Control Center
echo Starting LocalHelpBot Ecosystem...

:: 1. Start the Agentic Proxy and Web UI in the background
start /b venv\Scripts\python core/proxy.py

:: 2. Start the Discord Gateway in the background
start /b venv\Scripts\python core/discord_gateway.py

echo.
echo ---------------------------------------------------
echo  LocalHelpBot is now running!
echo  Web UI: http://localhost:11435
echo  Proxy:  http://localhost:11435/api/chat
echo ---------------------------------------------------
echo.

:: 3. Open the Web UI in the default browser
timeout /t 3 >nul
start http://localhost:11435

echo System ready. Close this window to stop the bot (or use Task Manager).
pause
