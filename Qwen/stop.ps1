$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'data\server.pid'
if (!(Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded server process.'; exit 0 }
$serverId = [int](Get-Content -LiteralPath $pidFile)
$process = Get-Process -Id $serverId -ErrorAction SilentlyContinue
$expected = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\ollama.exe'))
if ($process -and $process.Path -eq $expected) {
    Stop-Process -Id $serverId
    Write-Output 'Stopped this directory''s Ollama server.'
} else {
    Write-Output 'Recorded process is absent or does not match this directory; no process stopped.'
}
