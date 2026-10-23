@echo off
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\Qwen\.venv\Scripts\python.exe"
set "GUI=%~dp0gui.py"
if not exist "%PY%" (
  echo Cannot find %PY%
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Resolve-Path '%PY%'; $g=Resolve-Path '%GUI%'; Start-Process -Verb RunAs -FilePath $p.Path -ArgumentList @($g.Path) -WorkingDirectory (Split-Path $g.Path)"
endlocal
