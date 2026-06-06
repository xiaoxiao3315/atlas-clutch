@echo off
setlocal

cd /d "%~dp0"

if not exist runtime (
    mkdir runtime
)

> "runtime\stop.request" echo requested_at=%DATE% %TIME%

echo [bridge] Stop requested. The bridge will exit on its next poll cycle.
if exist "runtime\heartbeat.json" (
    echo [bridge] Current heartbeat:
    type "runtime\heartbeat.json"
) else (
    echo [bridge] No heartbeat found.
)

exit /b 0
