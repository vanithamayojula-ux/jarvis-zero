@echo off
title JARVIS Zero
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS Zero is not installed yet. Run install.ps1 first.
  pause
  exit /b 1
)
rem JARVIS only obeys requests carrying its secret token from .env (so other programs and websites can't command it)
set "JT="
for /f "tokens=1,* delims==" %%a in ('findstr /b "JARVIS_API_TOKEN=" .env 2^>nul') do set "JT=%%b"
if defined JT (start "" "http://127.0.0.1:8600/?token=%JT%") else (start "" http://127.0.0.1:8600)
start "" ".venv\Scripts\pythonw.exe" "scripts\bot_overlay.py"
cd backend
"..\.venv\Scripts\python.exe" -m jarvis
pause
