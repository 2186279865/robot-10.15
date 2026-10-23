# 需要管理员权限运行：放行机器人项目的入站端口，并把当前网络设为"专用"
# 用法：右键"以 PowerShell 管理员身份运行" -> 执行本脚本
$ErrorActionPreference = 'Stop'

Write-Host '=== 1) 当前网络设为专用（否则入站被拦） ===' -ForegroundColor Cyan
Get-NetConnectionProfile | ForEach-Object {
    Write-Host ("  {0}  ({1})  -> Private" -f $_.Name, $_.NetworkCategory)
    try { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }
    catch { Write-Warning ("  跳过 {0}: {1}" -f $_.Name, $_.Exception.Message) }
}

Write-Host '=== 2) 放行 TCP 5055-5059 / UDP 5056 ===' -ForegroundColor Cyan
foreach ($rule in @(
    @{Name='Robot TCP 5055-5059'; Proto='TCP'; Port='5055-5059'},
    @{Name='Robot UDP 5056';      Proto='UDP'; Port='5056'}
)) {
    Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow `
        -Protocol $rule.Proto -LocalPort $rule.Port -Profile Private, Domain | Out-Null
    Write-Host ("  + {0}  {1}/{2}" -f $rule.Name, $rule.Proto, $rule.Port)
}

Write-Host '=== 3) 本机在热点/局域网里的地址 ===' -ForegroundColor Cyan
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' } |
    Select-Object InterfaceAlias, IPAddress, PrefixLength | Format-Table -AutoSize

Write-Host '完成。下一步：python 10.21\net\find_pi.py' -ForegroundColor Green
