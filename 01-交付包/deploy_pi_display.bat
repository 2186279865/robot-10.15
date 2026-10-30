@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  Deploy Pi battle screen: pi_display.py + presets/ + autostart
rem  Usage: double-click, then type the Pi password (orangepi)
rem  Prereq: Pi powered on, same subnet (default 192.168.100.198)
rem
rem  2026-10-31 notes:
rem    - item photos come from presets/ (no more live frame capture)
rem    - rebuild 52/56/96/160 px copies from ..\..\图片\presets\*.png:
rem        %~dp0..\01-交付包\.venv\Scripts\python.exe %~dp0tools\make_presets.py
rem    - this script VERIFIES the upload (md5 + preset count) because the Pi
rem      got silently overwritten by an old copy once (screen showed plain
rem      color squares). If verify fails it stops instead of saying "done".
rem ============================================================

set PI=192.168.100.198
set PIUSER=orangepi
set LOCALDIR=%~dp0
set REMOTE=/home/orangepi/robot1021
set SSHOPT=-o StrictHostKeyChecking=no -o ConnectTimeout=6

echo.
echo [1/7] ping %PI% ...
ping -n 2 %PI% >nul 2>&1
if errorlevel 1 (
  echo   [x] ping failed: check Pi power / same subnet
  goto :fail
)
echo   [ok] network ok

echo.
echo [2/7] upload pi_display.py ...
scp %SSHOPT% "%LOCALDIR%orangepi\pi\pi_display.py" %PIUSER%@%PI%:%REMOTE%/pi/pi_display.py
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo [3/7] upload presets/ ^(24 png + index.json^) ...
scp %SSHOPT% -r "%LOCALDIR%orangepi\pi\presets" %PIUSER%@%PI%:%REMOTE%/pi/
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo [4/7] upload autostart entry ^(screen comes back after reboot^) ...
scp %SSHOPT% "%LOCALDIR%orangepi\pi\pi_display_autostart.desktop" %PIUSER%@%PI%:%REMOTE%/pi/pi_display_autostart.desktop
if errorlevel 1 goto :fail
ssh %SSHOPT% %PIUSER%@%PI% "mkdir -p ~/.config/autostart && cp %REMOTE%/pi/pi_display_autostart.desktop ~/.config/autostart/pi_display.desktop && chmod +x %REMOTE%/pi/start_display.sh"
if errorlevel 1 goto :fail
echo   [ok]

echo.
echo [5/7] verify upload ...
set RMHASH=
set LOHASH=
for /f "tokens=1" %%A in ('ssh %SSHOPT% %PIUSER%@%PI% "md5sum %REMOTE%/pi/pi_display.py"') do set RMHASH=%%A
for /f "skip=1 tokens=*" %%A in ('certutil -hashfile "%LOCALDIR%orangepi\pi\pi_display.py" MD5') do if not defined LOHASH set LOHASH=%%A
set RMHASH=%RMHASH: =%
set LOHASH=%LOHASH: =%
echo   local  md5 = %LOHASH%
echo   remote md5 = %RMHASH%
if /i not "%LOHASH%"=="%RMHASH%" (
  echo   [x] md5 mismatch: upload did NOT land (check permissions / disk full)
  goto :fail
)
for /f %%A in ('ssh %SSHOPT% %PIUSER%@%PI% "ls %REMOTE%/pi/presets/*.png 2>/dev/null ^| wc -l"') do set PNGCNT=%%A
echo   remote presets png = %PNGCNT%
if %PNGCNT% LSS 20 (
  echo   [x] preset images missing on the Pi: run tools\make_presets.py then re-run me
  goto :fail
)
for /f "delims=" %%A in ('ssh %SSHOPT% %PIUSER%@%PI% "grep -c '' %REMOTE%/pi/presets/index.json"') do set IDXLINES=%%A
echo   index.json lines = %IDXLINES%
if %IDXLINES% LSS 10 (
  echo   [x] presets/index.json looks empty/truncated
  goto :fail
)
echo   [ok] upload verified

echo.
echo [6/7] restart display ...
rem Pi-side start_display.sh kills the old pi_display.py and relaunches it detached.
ssh %SSHOPT% %PIUSER%@%PI% "cd %REMOTE%/pi && setsid nohup sh ./start_display.sh >/tmp/pdisp.out 2>&1 < /dev/null &"
if errorlevel 1 goto :fail
timeout /t 3 /nobreak >nul
ssh %SSHOPT% %PIUSER%@%PI% "ss -ltn 2>/dev/null | grep -q ':5059' || netstat -ltn 2>/dev/null | grep -q ':5059'"
if errorlevel 1 (
  echo   [x] port 5059 not listening after restart: see /tmp/pdisp.out on the Pi
  goto :fail
)
echo   [ok] display running (5059)
for /f "delims=" %%A in ('ssh %SSHOPT% %PIUSER%@%PI% "tail -3 /tmp/pdisp.out"') do echo   ^| %%A

echo.
echo [7/7] done
echo ============================================================
echo  OK: Pi screen = pi_display.py %RMHASH% + %PNGCNT% preset png
echo      Screen auto-starts after reboot (~/.config/autostart).
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [x] FAILED: see output above (wrong password / Pi offline / IP changed /
echo     presets not generated / upload blocked). Fix it, then re-run.
pause
exit /b 1
