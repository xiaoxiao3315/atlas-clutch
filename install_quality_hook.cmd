@echo off
rem Install the pre-push quality-gate hook (blocks pushes that fail the 13-check suite).
cd /d "%~dp0"
python -B scripts\install_quality_hook.py
pause
