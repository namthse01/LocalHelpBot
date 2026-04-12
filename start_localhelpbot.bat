@echo off
title LocalHelpBot Control Center
echo Starting LocalHelpBot...

:: Start the RAG Proxy and Web UI
start /b venv\Scripts\python core/proxy.py

echo.
echo ---------------------------------------------------
echo  LocalHelpBot is now running!
echo  Web UI: http://localhost:11435
echo  Proxy:  http://localhost:11435/api/chat
echo ---------------------------------------------------
echo  Discord/3rd-party: connect via Web UI > Connect tab
echo ---------------------------------------------------
echo.

:: Open the Web UI in the default browser
timeout /t 3 >nul
start http://localhost:11435

echo System ready. Close this window to stop the bot.
pause
