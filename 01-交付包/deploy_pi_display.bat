@echo off
setlocal

rem ============================================================
rem  Deploy Pi battle screen: pi_display.py + presets/
rem  Usage: double-click, then type the Pi password (orangepi)
rem  Prereq: Pi powered on, same subnet (default 192.168.100.198)
rem
rem  Note (2026-10-31): the on-screen item photo now comes from
rem  presets/ (no more live frame capture). To rebuild the
rem  96/160 px copies from 图片\presets\*.png run:
rem      %~dp0..\01-交付包\.venv\Scripts\python.exe %~dp0tools\make_presets.py
rem ============================================================

set PI=192.168.100.198
set PIUSER=orangepi
set LOCALDIR=%~dp0
set REMOTE=/home/orangepi/robot1021

echo.
echo [1/4] ping %PI% ...
ping -n 2 %PI% >nul 2>&1
if errorlevel 1 (
  echo   [x] ping failed: check Pi power / same subnet
  goto :fail
)
echo   [ok] network ok

echo.
echo [2/4] upload pi_display.py ...
scp -o StrictHostKeyChecking=no "%LOCALDIR%orangepi\pi\pi_display.py" %PIUSER%@%PI%:%REMOTE%/pi/pi_display.py
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo [3/4] upload presets/ ...
scp -o StrictHostKeyChecking=no -r "%LOCALDIR%orangepi\pi\presets" %PIUSER%@%PI%:%REMOTE%/pi/
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo [4/4] restart display ...
rem Use the Pi-side start_display.sh (it kills the old pi_display.py first and
rem re-launches it detached, so ssh returns immediately). The old target
rem "..\.survey\start_disp.sh" never existed -> this step used to fail.
ssh -o StrictHostKeyChecking=no %PIUSER%@%PI% "cd %REMOTE%/pi && setsid nohup sh ./start_display.sh >/tmp/pi_display.out 2>&1 < /dev/null &"
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo ============================================================
echo  DONE: Pi screen updated (pi_display.py + preset images)
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [x] FAILED: see scp/ssh output above (wrong password / Pi offline / IP changed)
pause
exit /b 1
