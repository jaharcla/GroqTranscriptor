@echo off
set "BRIDGE_DIR=%~dp0"
cd /d "%BRIDGE_DIR%"
if exist .venv\Scripts\courseai-listener.exe (
    start "" "C:\CourseAI\Lectures\Audio Inbox"
    start "" "%BRIDGE_DIR%.venv\Scripts\courseai-listener.exe"
    exit /b 0
)

if exist .venv\Scripts\python.exe (
    start "" "C:\CourseAI\Lectures\Audio Inbox"
    .venv\Scripts\python.exe -m courseai_lectures.listener_ui
    exit /b %errorlevel%
)

echo CourseAI Listener is not installed in C:\CourseAI\Bridge. Run Install-Update.ps1 first.
pause
exit /b 1
