@echo off
title Spidey
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Spidey is not installed yet. Run install.ps1 first.
  pause
  exit /b 1
)
rem spidey with JARVIS's small panel (the full dashboard is a click away in the panel, or http://127.0.0.1:8600)
start "" ".venv\Scripts\pythonw.exe" "scripts\bot_overlay.py" --panel
cd backend
"..\.venv\Scripts\python.exe" -m jarvis
pause
