# 通过串口 COM9 给香橙派发关机命令，并读取控制台回显
$ErrorActionPreference = 'Continue'
$port = [System.IO.Ports.SerialPort]::new('COM9', 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$port.NewLine = "`r`n"
$port.ReadTimeout = 500
$port.WriteTimeout = 1000

try {
    $port.Open()
} catch {
    Write-Host "[ERROR] 打不开 COM9：$($_.Exception.Message)"
    exit 2
}
Start-Sleep -Milliseconds 300
$port.DiscardInBuffer()

function Read-For([int]$ms) {
    $end = (Get-Date).AddMilliseconds($ms)
    $sb = New-Object System.Text.StringBuilder
    while ((Get-Date) -lt $end) {
        try {
            $n = $port.BytesToRead
            if ($n -gt 0) {
                $buf = New-Object byte[] $n
                [void]$port.Read($buf, 0, $n)
                [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buf))
            } else { Start-Sleep -Milliseconds 50 }
        } catch { Start-Sleep -Milliseconds 50 }
    }
    return $sb.ToString()
}

Write-Host "===== 1) 敲回车取提示符 ====="
$port.Write("`r")
$out1 = Read-For 1200
Write-Host $out1

Write-Host "===== 2) 发送 sudo shutdown -h now ====="
$port.Write("sudo shutdown -h now`r")
$out2 = Read-For 2500
Write-Host $out2

if ($out2 -match '(?i)password') {
    Write-Host "===== 3) 发送 sudo 密码 ====="
    $port.Write("orangepi`r")
}

Write-Host "===== 4) 等关机输出（最多 40s）====="
$all = Read-For 40000
Write-Host $all

$port.Close()
Write-Host "===== 串口已关闭 ====="
