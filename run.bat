@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" bridge.py %*
) else (
  python bridge.py %*
)
pause
