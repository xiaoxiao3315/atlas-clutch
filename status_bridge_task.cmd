@echo off
setlocal

cd /d "%~dp0"
set "TASK_NAME=OctoHermesBridge"

echo [bridge-task] Scheduled task status: %TASK_NAME%
schtasks /Query /TN "%TASK_NAME%" /V /FO LIST
set TASK_STATUS=%ERRORLEVEL%

echo.
echo [bridge-task] Runtime heartbeat:
if exist "runtime\heartbeat.json" (
    type "runtime\heartbeat.json"
) else (
    echo runtime\heartbeat.json not found.
)

echo.
echo [bridge-task] Recent bridge log:
if exist "logs\bridge.log" (
    powershell -NoProfile -Command "Get-Content -Path 'logs\bridge.log' -Tail 20"
) else (
    echo logs\bridge.log not found.
)

exit /b %TASK_STATUS%
