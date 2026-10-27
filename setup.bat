@echo off
title JARVIS Zero setup
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS Zero is not installed yet. Run install.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\setup.py
pause
