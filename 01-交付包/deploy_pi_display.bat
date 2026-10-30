@echo off
setlocal

rem ============================================================
rem  Deploy Pi battle screen: pi_display.py + presets/
rem  Usage: double-click, then type the Pi password (orangepi)
rem  Prereq: Pi powered on, same subnet (default 192.168.100.198)
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
ssh -o StrictHostKeyChecking=no %PIUSER%@%PI% "bash -s" < "%LOCALDIR%..\.survey\start_disp.sh"
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  DONE: Pi screen updated (pi_display.py + 6 preset images)
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [x] FAILED: see scp/ssh output above (wrong password / Pi offline / IP changed)
pause
exit /b 1