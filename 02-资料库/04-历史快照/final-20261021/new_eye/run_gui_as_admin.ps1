$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project 'Qwen\.venv\Scripts\python.exe'
$gui = Join-Path $PSScriptRoot 'gui.py'
if (-not (Test-Path -LiteralPath $python)) { throw "找不到 Python: $python" }
Start-Process -Verb RunAs -FilePath $python -ArgumentList @($gui) -WorkingDirectory $PSScriptRoot
