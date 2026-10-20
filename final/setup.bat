@echo off
rem 首次部署：在 final 目录建立独立虚拟环境并安装依赖（只做一次）
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 python，请先安装 Python 3.10~3.12 并勾选 Add to PATH
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo === 创建虚拟环境 .venv ===
  python -m venv .venv || (echo [错误] 创建虚拟环境失败 & pause & exit /b 1)
)

echo === 升级 pip ===
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo === 安装依赖（torch 默认装 CPU 版；有 NVIDIA 显卡建议换 CUDA 版）===
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo [错误] 依赖安装失败 & pause & exit /b 1)

echo.
echo 完成。之后直接双击 run.bat 启动。
echo 若要用 GPU 加速，先执行：
echo   .venv\Scripts\python.exe -m pip uninstall -y torch torchvision
echo   .venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
pause
