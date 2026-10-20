@echo off
rem 一键启动：视觉 + 控制 一体化软件
rem 双击运行。Python 解释器按下面顺序找：
rem   1) final\.venv\Scripts\python.exe （先跑 setup.bat 生成）
rem   2) ..\Qwen\.venv\Scripts\python.exe （旧环境，能用就不重装）
rem   3) PATH 里的 python
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\Qwen\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 使用解释器：%PY%
"%PY%" main.py %*
if errorlevel 1 pause
endlocal
