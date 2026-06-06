@echo off
setlocal

cd /d "%~dp0"

if /I "%~1"=="task" (
    set "OHB_START_METHOD=task"
) else if "%OHB_START_METHOD%"=="" (
    set "OHB_START_METHOD=manual"
)

if not exist logs (
    mkdir logs
)

if not exist runtime (
    mkdir runtime
)

where python >nul 2>nul
if errorlevel 1 (
    echo [bridge] Python was not found in PATH.
    echo [bridge] Install Python or add it to PATH, then run this file again.
    if /I not "%OHB_START_METHOD%"=="task" pause
    exit /b 1
)

echo [bridge] Starting Octo-Hermes Bridge from %CD% [%OHB_START_METHOD%]
python bridge.py
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
    echo [bridge] Bridge exited with code %EXIT_CODE%.
    if "%EXIT_CODE%"=="2" exit /b %EXIT_CODE%
    if /I not "%OHB_START_METHOD%"=="task" pause
)

exit /b %EXIT_CODE%
