@echo off
rem One-click launch: vision + control (integrated GUI).
rem Keep this file pure ASCII with CRLF line endings.
rem cmd.exe parses .bat byte-wise under the system ANSI codepage;
rem UTF-8 Chinese or LF-only line endings break line boundaries.
rem
rem Usage:
rem   run.bat                      local mode (USB cameras + USB serial)
rem   run.bat --pi 192.168.137.50  WiFi mode (cameras + STM32 on the Pi)
rem   run.bat --sim                simulator, no hardware
rem Interpreter lookup order (fully self-contained package, no outside deps):
rem   1) .venv  (created by setup.bat -- recommended, see README)
rem   2) python on PATH
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Using interpreter: %PY%
"%PY%" main.py %*
if errorlevel 1 pause
endlocal
