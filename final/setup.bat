@echo off
rem One-time setup: create .venv in this folder and install requirements.
rem NOTE: this file must stay pure ASCII with CRLF line endings.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found - install Python 3.10-3.12 and tick Add-to-PATH
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo === creating virtualenv .venv ===
  python -m venv .venv || (echo [ERROR] failed to create virtualenv & pause & exit /b 1)
)

echo === upgrading pip ===
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo === installing requirements ===
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo [ERROR] pip install failed & pause & exit /b 1)

echo.
echo Done. Double-click run.bat from now on.
echo For GPU acceleration run:
echo   .venv\Scripts\python.exe -m pip uninstall -y torch torchvision
echo   .venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
pause
