# 認證取樣 Field Test 用：唯讀取樣服務程序的 CPU 累計秒數與記憶體（Working Set），每 N 秒一行 CSV。
# 不改任何設定、不碰服務；Ctrl+C 結束。
# 用法：pwsh -File cert_proc_sampler.ps1 -ServicePid <pid> -Out <csv> [-IntervalSec 10]
#   pid 取自 output\cert_reports\status.json 的 "pid"
param([Parameter(Mandatory = $true)][int]$ServicePid, [Parameter(Mandatory = $true)][string]$Out, [int]$IntervalSec = 10)
"time,cpu_s,working_set_mb" | Out-File -FilePath $Out -Encoding utf8
while ($true) {
    $p = Get-Process -Id $ServicePid -ErrorAction SilentlyContinue
    if (-not $p) { Write-Host "程序 $ServicePid 已不存在，停止取樣"; break }
    "{0},{1:F2},{2:F1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $p.CPU, ($p.WorkingSet64 / 1MB) |
        Out-File -FilePath $Out -Append -Encoding utf8
    Start-Sleep -Seconds $IntervalSec
}
