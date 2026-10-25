@echo off
rem ===========================================================================
rem  10.21 self-test: run every automated check in this package, in order.
rem  Keep this file pure ASCII with CRLF line endings (cmd.exe parses .bat
rem  byte-wise under the system ANSI codepage).
rem
rem  Usage:  selftest.bat            full run (~3-5 min, opens 2 GUI windows)
rem          selftest.bat /quick     headless steps only
rem
rem  Requires: deps installed (setup.bat). No hardware needed.
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\Qwen\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "FAILED=0"
set "QUICK=0"
if /i "%~1"=="/quick" set "QUICK=1"

echo.
echo ============================================================
echo  10.21 self-test
echo  interpreter: %PY%
echo ============================================================

rem ---------------------------------------------------------------- 1
echo.
echo [1/7] host unit tests (expect 182 OK)
pushd host
"%PY%" -m unittest discover -s tests -t tests -q
set "RC=%errorlevel%"
popd
set "NAME=host unit tests"
call :check
if not "%RC%"=="0" goto :summary

rem ---------------------------------------------------------------- 2
echo.
echo [2/7] vision package imports + SAM weights
"%PY%" tests\check_vision_imports.py
set "RC=%errorlevel%"
set "NAME=vision imports"
call :check
if not "%RC%"=="0" goto :summary

rem ---------------------------------------------------------------- 3
echo.
echo [3/7] start fake Pi agent (15057/15058)
rem  Spawn detached through PowerShell: a plain "start" would let the agent
rem  inherit this console, so a caller piping this script's output would never
rem  see EOF. Detached + redirected also keeps the log for debugging.
if not exist "runtime" mkdir "runtime"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath '%PY%' -ArgumentList 'orangepi\pi\pi_agent.py','--fake-camera','--fake-serial','--img-port','15057','--ser-port','15058','--no-discovery' -PassThru -WindowStyle Hidden -RedirectStandardOutput 'runtime\pi_agent.selftest.log' -RedirectStandardError 'runtime\pi_agent.selftest.err'; $p.Id | Out-File -FilePath 'runtime\pi_agent.pid' -Encoding ascii"
"%PY%" tests\wait_ports.py 15057 15058
set "RC=%errorlevel%"
set "NAME=fake Pi agent up"
call :check
if not "%RC%"=="0" (
  call :stop_agent
  goto :summary
)

rem ---------------------------------------------------------------- 4
echo.
echo [4/7] Pi-side selftest client (expect 24 PASS)
"%PY%" orangepi\pi\tests\selftest_client.py --host 127.0.0.1 --img-port 15057 --ser-port 15058
set "RC=%errorlevel%"
set "NAME=pi selftest_client"
call :check

rem ---------------------------------------------------------------- 5
echo.
echo [5/7] Pi to real-host protocol interop (expect 37 PASS)
"%PY%" orangepi\pi\tests\test_host_protocol.py --host 127.0.0.1 --ser-port 15058
set "RC=%errorlevel%"
set "NAME=pi host-protocol interop"
call :check

echo.
echo   stopping fake Pi agent
call :stop_agent
"%PY%" tests\wait_ports.py --closed 15057 15058
rem  not "timeout": it fails when stdin is redirected (CI, pipes).
ping -n 2 127.0.0.1 >nul

rem ---------------------------------------------------------------- 6
echo.
echo [6/7] laptop end-to-end over TCP (spawns own agent, expect 5 PASS)
"%PY%" tests\test_net_local.py
set "RC=%errorlevel%"
set "NAME=test_net_local"
call :check

rem ---------------------------------------------------------------- 7
if "%QUICK%"=="1" (
  echo.
  echo [7/7] skipped GUI steps because of /quick
  goto :summary
)

echo.
echo [7/7] GUI smoke (2 windows, ~30 s)
"%PY%" tests\gui_remote_smoke.py
set "RC=%errorlevel%"
set "NAME=gui_remote_smoke"
call :check

"%PY%" main.py --selftest
set "RC=%errorlevel%"
set "NAME=main.py --selftest"
call :check

:summary
echo.
echo ============================================================
if "%FAILED%"=="0" (
  echo  RESULT: ALL CHECKS PASSED
  echo ============================================================
  endlocal
  exit /b 0
)
echo  RESULT: SOME CHECKS FAILED - see [FAIL] lines above
echo ============================================================
pause
endlocal
exit /b 1

:check
if "%RC%"=="0" (
  echo   [ OK ] %NAME%
) else (
  set "FAILED=1"
  echo   [FAIL] %NAME%
)
goto :eof

:stop_agent
if not exist "runtime\pi_agent.pid" goto :eof
powershell -NoProfile -ExecutionPolicy Bypass -Command "$id = (Get-Content 'runtime\pi_agent.pid' -Raw).Trim(); if ($id) { Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue }"
del /q "runtime\pi_agent.pid" >nul 2>&1
goto :eof
