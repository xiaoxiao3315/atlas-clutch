@echo off
setlocal

cd /d "%~dp0"
set "TASK_NAME=OctoHermesBridge"

echo [bridge-task] Requesting graceful bridge stop.
call "%~dp0stop_bridge.cmd"
timeout /t 5 /nobreak >nul

echo [bridge-task] Stopping scheduled task if it is still running: %TASK_NAME%
schtasks /End /TN "%TASK_NAME%" >nul 2>nul

echo [bridge-task] Deleting scheduled task: %TASK_NAME%
schtasks /Delete /TN "%TASK_NAME%" /F
if errorlevel 1 (
    echo [bridge-task] Delete failed or task was not found.
    exit /b 1
)

echo [bridge-task] Deleted.
exit /b 0
