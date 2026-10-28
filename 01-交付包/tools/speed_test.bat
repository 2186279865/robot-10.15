@echo off
cd /d "%~dp0.."
echo === Y axis (800/1000/1200 rpm, 2000 pulses) ===
".venv\Scripts\python.exe" tools\speed_test.py --axis 5 --rpm 800 1000 1200 --pulses 2000
echo.
echo === Z axis (1500/2000/2200 rpm, 20000 pulses) ===
".venv\Scripts\python.exe" tools\speed_test.py --axis 1 --rpm 1500 2000 2200 --pulses 20000
echo.
pause