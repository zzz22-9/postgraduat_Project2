@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [FAIL] Project Python is missing. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -X utf8 tools\demo_launcher.py --check --no-browser
set "demoExit=%ERRORLEVEL%"
pause
exit /b %demoExit%
