@echo off
rem Launch the robot host GUI.
rem Prefer the Python installed by winget into the user profile, else fall back to PATH.
setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0host_gui.py" %*
if errorlevel 1 pause
