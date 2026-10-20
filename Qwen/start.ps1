$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:OLLAMA_MODELS = Join-Path $root 'models'
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_CONTEXT_LENGTH = '4096'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_KEEP_ALIVE = '30m'
$env:USERPROFILE = Join-Path $root 'home'
$env:TEMP = Join-Path $root 'data'
$env:TMP = $env:TEMP
$exe = Join-Path $root 'runtime\ollama.exe'
if (!(Test-Path -LiteralPath $exe)) { throw 'Run setup.ps1 first.' }
try {
    $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
    Write-Output 'Ollama is already listening on port 11434. Verify its model directory before using it.'
    exit 0
} catch {}
$process = Start-Process -FilePath $exe -ArgumentList 'serve' -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\server.stdout.log') -RedirectStandardError (Join-Path $root 'logs\server.stderr.log')
$process.Id | Set-Content (Join-Path $root 'data\server.pid')
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $version = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
        Write-Output "Ollama ready: $($version.version), models: $env:OLLAMA_MODELS"
        exit 0
    } catch {}
}
throw 'Server did not start. Check logs/server.stderr.log.'
