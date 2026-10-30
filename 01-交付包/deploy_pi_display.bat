@echo off
chcp 65001 >nul
setlocal
set "PI=orangepi@192.168.100.198"
set "REMOTE=/home/orangepi/robot1021/pi"
set "LOCAL=%~dp0orangepi\pi\pi_display.py"
set "START=%~dp0..\.survey\start_disp.sh"

echo ============================================
echo  上传派端比赛屏 pi_display.py 并重启（要输密码）
echo  目标：%PI%:%REMOTE%
echo ============================================
echo.

if not exist "%LOCAL%" (
  echo [X] 找不到本地上位机屏脚本：%LOCAL%
  pause
  exit /b 1
)

echo [1/2] 上传 pi_display.py ...
scp "%LOCAL%" %PI%:%REMOTE%/pi_display.py
if errorlevel 1 goto :err

echo.
echo [2/2] 重启派端显示 ...
ssh %PI% "bash -s" < "%START%"
if errorlevel 1 goto :err

echo.
echo [OK] 派端屏已用新版启动（看派端屏幕是否显示「运行中/待机」）。
pause
exit /b 0

:err
echo.
echo [X] 失败：检查派是否上电/同一 WiFi、用户名密码是否正确。
echo     手动方式：WinSCP 把 orangepi\pi\pi_display.py 传到 %REMOTE%
echo               再在派上执行：bash ~/robot1021/start_disp.sh（或 .survey\start_disp.sh）
pause
exit /b 1
