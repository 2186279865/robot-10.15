@echo off
rem Teammate one-click self check.
rem Keep this file pure ASCII with CRLF: cmd.exe parses .bat byte-wise,
rem so non-ASCII text or LF-only line endings break line boundaries.
rem The deliverable folder name is Chinese, so we discover it with a wildcard
rem instead of hardcoding it here.
setlocal
cd /d "%~dp0.."
set "PKG1="
for /d %%D in ("%~dp0..\01-*") do if not defined PKG1 set "PKG1=%%D"
set "PY=%PKG1%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Using interpreter: %PY%
"%PY%" "%~dp0tools\check_teammate.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" pause
endlocal & exit /b %RC%
