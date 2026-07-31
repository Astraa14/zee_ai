@echo off
title JARVIS - Local Assistant
cd /d "%~dp0"

echo ============================================
echo   JARVIS launcher
echo ============================================

REM --- Kill any stale server still holding port 5000 ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    echo Stopping old process PID %%a holding port 5000...
    taskkill /F /PID %%a >nul 2>&1
)

REM --- Start JARVIS over HTTPS (mic works from any device) ---
set JARVIS_HTTPS=1
set FLASK_DEBUG=0
python app.py
pause
