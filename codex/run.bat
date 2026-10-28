@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "ROBOT_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%ROBOT_PY%" for /d %%D in ("%~dp0..\01-*") do set "ROBOT_PY=%%~fD\.venv\Scripts\python.exe"
if not exist "%ROBOT_PY%" (
  echo Python environment missing. Run setup.bat first.
  pause
  exit /b 1
)
"%ROBOT_PY%" main.py %*
if errorlevel 1 pause
