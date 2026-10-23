@echo off
rem One-click launcher: integrated vision + control application.
rem Double-click to run. Python interpreter lookup order:
rem   1) final\.venv\Scripts\python.exe   (created by setup.bat)
rem   2) ..\Qwen\.venv\Scripts\python.exe  (legacy env, reuse if usable)
rem   3) python from PATH
rem NOTE: this file must stay pure ASCII with CRLF line endings.
rem       cmd.exe parses batch files with the ANSI codepage; UTF-8 Chinese
rem       text here breaks line parsing ("m is not recognized" etc).
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\Qwen\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Using interpreter: %PY%
"%PY%" main.py %*
if errorlevel 1 pause
endlocal
