$log = Join-Path $PSScriptRoot 'net-switch.log'
"[$(Get-Date -Format s)] 切回 iQOO 13" | Out-File -FilePath $log -Append -Encoding utf8
$out = netsh wlan connect name="iQOO 13" 2>&1 | Out-String
$out.Trim() | Out-File -FilePath $log -Append -Encoding utf8
Start-Sleep -Seconds 10
(ipconfig | Out-String) -split "`r?`n" | Where-Object { $_ -match 'IPv4|Default Gateway' } |
  Out-File -FilePath $log -Append -Encoding utf8
"[$(Get-Date -Format s)] 结束" | Out-File -FilePath $log -Append -Encoding utf8
