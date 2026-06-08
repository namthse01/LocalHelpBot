@echo off
:: TheAgent0 — one-click installer for Windows.
:: Double-click this file, or run it from a terminal.
setlocal
cd /d "%~dp0"

:: Prefer the py launcher (ships with python.org installs), fall back to python.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 install.py %*
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python install.py %*
    goto :done
)

echo [error] Python 3.10+ was not found on PATH.
echo         Install it from https://www.python.org/downloads/ (tick
echo         "Add python.exe to PATH"), then run this file again.
pause
exit /b 1

:done
echo.
pause
