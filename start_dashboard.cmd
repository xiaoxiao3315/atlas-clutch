@echo off
setlocal

cd /d "%~dp0"

set "DASHBOARD_PORT=8765"
if not "%~1"=="" set "DASHBOARD_PORT=%~1"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo [dashboard] Starting Atlas Workbench Dashboard
echo [dashboard] URL: http://127.0.0.1:%DASHBOARD_PORT%/
echo [dashboard] mode: read_only_dashboard
echo [dashboard] bind: 127.0.0.1
echo [dashboard] external_access: false
echo [dashboard] auto_execute_enabled: false
echo [dashboard] Stop with Ctrl+C.

"%PYTHON_EXE%" dashboard_server.py --host 127.0.0.1 --port %DASHBOARD_PORT%
exit /b %ERRORLEVEL%
