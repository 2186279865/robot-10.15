@echo off
rem Create a local virtual environment for 10.21 and install dependencies.
rem Keep this file pure ASCII with CRLF line endings.
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found - install Python 3.10-3.12 and tick Add-to-PATH
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo [2/3] upgrading pip ...
"%VPY%" -m pip install --upgrade pip

echo [3/3] installing requirements.txt ...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo Done. Start with: run.bat
echo   - simulator:  run.bat --sim
echo   - WiFi / Pi:  run.bat --pi 192.168.137.50
exit /b 0

:fail
echo [ERROR] install failed - see messages above
pause
exit /b 1
