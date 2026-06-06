@echo off
setlocal

cd /d "%~dp0"
set "TASK_NAME=OctoHermesBridge"

echo [bridge-task] Installing scheduled task: %TASK_NAME%

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$taskName='%TASK_NAME%';" ^
  "$root=(Resolve-Path '.').Path;" ^
  "$script=Join-Path $root 'start_bridge.cmd';" ^
  "$action=New-ScheduledTaskAction -Execute $script -Argument 'task' -WorkingDirectory $root;" ^
  "$trigger=New-ScheduledTaskTrigger -AtLogOn;" ^
  "$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited;" ^
  "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Description 'Starts Octo-Hermes Bridge at user logon.' -Force | Out-Null;"

if errorlevel 1 (
    echo [bridge-task] ScheduledTasks cmdlet failed. Trying schtasks.exe fallback...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$taskName='%TASK_NAME%';" ^
      "$script=(Resolve-Path '.\start_bridge.cmd').Path;" ^
      "$taskRun='\"' + $script + '\" task';" ^
      "& schtasks.exe /Create /TN $taskName /SC ONLOGON /TR $taskRun /F;" ^
      "exit $LASTEXITCODE;"
)

if errorlevel 1 (
    echo [bridge-task] Install failed. Current user may not have permission to create scheduled tasks.
    exit /b 1
)

echo [bridge-task] Installed. The bridge will start when the current user logs in.
call "%~dp0status_bridge_task.cmd"
exit /b %ERRORLEVEL%
