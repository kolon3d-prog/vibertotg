@echo off
cd /d "%~dp0"
title ViberToTG
:waitviber
tasklist /FI "IMAGENAME eq Viber.exe" | find /I "Viber.exe" >nul
if errorlevel 1 (
  timeout /t 5 /nobreak >nul
  goto waitviber
)
timeout /t 8 /nobreak >nul
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" bridge.py
) else (
  python bridge.py
)
