@echo off
setlocal

cd /d "%~dp0"

if not exist logs (
    mkdir logs
)

where python >nul 2>nul
if errorlevel 1 (
    echo [bridge] Python was not found in PATH.
    echo [bridge] Install Python or add it to PATH, then run this file again.
    pause
    exit /b 1
)

echo [bridge] Starting Octo-Hermes Bridge from %CD%
python bridge.py
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
    echo [bridge] Bridge exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
