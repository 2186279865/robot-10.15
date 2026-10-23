param(
  [Parameter(Mandatory = $true)][string]$Profile
)
# 以管理员身份切换 WiFi（netsh wlan connect 需要提权）。结果写日志，便于普通会话读取。
$log = Join-Path $PSScriptRoot 'net-switch.log'
"[$(Get-Date -Format s)] 请求切换到 $Profile" | Out-File -FilePath $log -Append -Encoding utf8
$out = netsh wlan connect name="$Profile" 2>&1 | Out-String
$out.Trim() | Out-File -FilePath $log -Append -Encoding utf8
Start-Sleep -Seconds 8
(ipconfig | Out-String) -split "`r?`n" | Where-Object { $_ -match 'IPv4|默认网关|Default Gateway' } |
  Out-File -FilePath $log -Append -Encoding utf8
"[$(Get-Date -Format s)] 结束" | Out-File -FilePath $log -Append -Encoding utf8
