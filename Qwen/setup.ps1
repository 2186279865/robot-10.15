$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
foreach ($name in @('downloads','runtime','models','logs','data','samples','home')) {
    $null = New-Item -ItemType Directory -Force (Join-Path $root $name)
}
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Python 3.12 not found. Set the python path in setup.ps1.' }
if (!(Test-Path (Join-Path $root '.venv\Scripts\python.exe'))) {
    & $python -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create local Python environment.' }
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path (Join-Path $root 'runtime\ollama.exe'))) {
    & $python -u (Join-Path $root 'download_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw 'Runtime download failed; rerun to resume.' }
}
& $python -u (Join-Path $root 'download_model.py')
if ($LASTEXITCODE -ne 0) { throw 'Model download failed; rerun to resume.' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'start.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Server startup failed.' }
