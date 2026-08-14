# -*- coding: utf-8 -*-
<#
    Phase 4.6-A — Service 控制流程（stop / remove）情境驗證探針

    以 dot-source 取得 install_service_nssm.ps1 的函式後，注入樁函式模擬各種服務狀態，
    驗證 Stop-SvcIfRunning / Remove-SvcIdempotent 的行為。

    **完全不碰真實服務**：Get-SvcStatus 與 Invoke-Nssm 都被替換成樁，
    不會呼叫 nssm，也不會查詢或修改任何 Windows 服務。

    dot-source 時使用 -Action status（唯讀、免管理員）；該動作只查詢不存在的
    測試用服務名，不會影響 ESSAutoMonitor。

    結果以單行 JSON（前綴 __P46CTL__）輸出，供 Python 測試解析。
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installScript = Join-Path (Split-Path -Parent $here) 'tools\install_service_nssm.ps1'

# 用一個不存在的服務名 dot-source，確保 status 動作不觸及 ESSAutoMonitor
. $installScript -Action status -ServiceName '__P46_NOT_A_REAL_SERVICE__' `
    -NssmPath (Join-Path (Split-Path -Parent $here) 'tools\nssm.exe') | Out-Null

# ---- 樁：狀態序列與 nssm 呼叫紀錄 ----
$script:StatusQueue = @()
$script:NssmCalls = @()

function Get-SvcStatus {
    if ($script:StatusQueue.Count -eq 0) { return 'Absent' }
    $v = $script:StatusQueue[0]
    if ($script:StatusQueue.Count -gt 1) {
        $script:StatusQueue = $script:StatusQueue[1..($script:StatusQueue.Count - 1)]
    }
    return $v
}

$script:NssmExitToReturn = 0

function Invoke-Nssm {
    param([string[]]$NssmArgs)
    $script:NssmCalls += ($NssmArgs -join ' ')
    $script:LastNssmExit = $script:NssmExitToReturn
    return "(stub) $($NssmArgs -join ' ')"
}

function Reset-Stub {
    param([string[]]$Statuses, [int]$NssmExit = 0)
    $script:StatusQueue = $Statuses
    $script:NssmCalls = @()
    $script:NssmExitToReturn = $NssmExit
}

function Invoke-StartCase {
    <#
        跑一次 Start-SvcAndWait，回傳 @{ calls; threw; error; warnings }。
        以 3>&1 只擷取 warning 串流（Write-Host 走 information 串流，不會混進來）。
    #>
    $err = $null
    $warn = @()
    try {
        $warn = @(Start-SvcAndWait 3>&1 | ForEach-Object { $_.ToString() })
    } catch {
        $err = $_.Exception.Message
    }
    return @{
        calls    = $script:NssmCalls
        threw    = ($null -ne $err)
        error    = $err
        warnings = ($warn -join "`n")
    }
}

$results = @{}

# ---- A. Running → stop → remove ----
# 狀態查詢順序：
#   ① Remove-SvcIdempotent 的 Absent 檢查      → Running
#   ② Stop-SvcIfRunning 的狀態判斷            → Running（觸發 nssm stop）
#   ③ Stop-SvcIfRunning 的等待迴圈            → Stopped（停止成功）
#   ④ Remove-SvcIdempotent 的 remove 後確認    → Absent（移除成功）
Reset-Stub @('Running', 'Running', 'Stopped', 'Absent')
$errA = $null
try { Remove-SvcIdempotent | Out-Null } catch { $errA = $_.Exception.Message }
$results['A'] = @{
    calls = $script:NssmCalls
    threw = ($null -ne $errA)
    error = $errA
}

# ---- B. Stopped → 略過 stop → remove ----
Reset-Stub @('Stopped', 'Stopped', 'Absent')
$errB = $null
try { Remove-SvcIdempotent | Out-Null } catch { $errB = $_.Exception.Message }
$results['B'] = @{
    calls = $script:NssmCalls
    threw = ($null -ne $errB)
    error = $errB
}

# ---- C. 服務不存在 → idempotent success，完全不呼叫 nssm ----
Reset-Stub @('Absent')
$errC = $null
try { Remove-SvcIdempotent | Out-Null } catch { $errC = $_.Exception.Message }
$results['C'] = @{
    calls = $script:NssmCalls
    threw = ($null -ne $errC)
    error = $errC
}

# ---- D. stop 真失敗（一直是 Running）→ 必須 throw，且不得呼叫 remove ----
# StopWaitSec 暫時縮短，避免測試等待 40 秒
$script:StopWaitSec = 1
$StopWaitSec = 1
Reset-Stub @('Running')          # 佇列耗盡後固定回 'Absent'… 需固定 Running：用長序列
$script:StatusQueue = @('Running') * 200
$script:NssmCalls = @()
$errD = $null
try { Remove-SvcIdempotent | Out-Null } catch { $errD = $_.Exception.Message }
$results['D'] = @{
    calls = $script:NssmCalls
    threw = ($null -ne $errD)
    error = $errD
}

# ---- E. remove 真失敗（remove 後仍存在）→ 必須 throw ----
Reset-Stub @('Stopped', 'Stopped', 'Stopped')   # remove 後查詢仍非 Absent
$errE = $null
try { Remove-SvcIdempotent | Out-Null } catch { $errE = $_.Exception.Message }
$results['E'] = @{
    calls = $script:NssmCalls
    threw = ($null -ne $errE)
    error = $errE
}

# ---- F. Stop-SvcIfRunning 對不存在服務 → true 且不呼叫 nssm ----
Reset-Stub @('Absent')
$okF = Stop-SvcIfRunning
$results['F'] = @{ calls = $script:NssmCalls; ok = [bool]$okF }

# ---- G. Remove-NulChars：NUL 清除的各種輸入 ----
# 這是 Assert-InstalledSettings 實機崩潰的直接成因
#（.Replace([char]0,'') 會綁到 Replace(Char,Char) 而拋型別轉換例外）
$NUL = [char]0
$nulCases = [ordered]@{
    'no_nul'        = 'auto_monitor_service.py --interval 10'
    'middle_nul'    = ('abc' + $NUL + 'def')
    'trailing_nul'  = ('abc' + $NUL)
    'leading_nul'   = ($NUL + 'abc')
    'only_nul'      = ($NUL + $NUL)
    'empty'         = ''
    'appparams'     = ('auto_monitor_service.py --interval 10' + $NUL)
    'appdir_space'  = ('D:\Crawler Sample\test' + $NUL)
}
$g = [ordered]@{}
foreach ($k in $nulCases.Keys) {
    $entry = @{ threw = $false; error = $null; out = $null; hasNul = $null }
    try {
        $r = Remove-NulChars $nulCases[$k]
        $entry.out = $r
        $entry.hasNul = $r.Contains($NUL)
    } catch {
        $entry.threw = $true
        $entry.error = $_.Exception.Message.Split("`n")[0]
    }
    $g[$k] = $entry
}
# null 輸入亦不得拋例外
try {
    $rn = Remove-NulChars $null
    $g['null_input'] = @{ threw = $false; error = $null; out = $rn; hasNul = $false }
} catch {
    $g['null_input'] = @{ threw = $true; error = $_.Exception.Message.Split("`n")[0] }
}
$results['G'] = $g

# ---- H. 確認舊寫法確實會炸（守住根因，避免日後改回去）----
$oldThrew = $false
$oldErr = $null
try {
    $null = ('abc' + $NUL).Replace([char]0, '')
} catch {
    $oldThrew = $true
    $oldErr = $_.FullyQualifiedErrorId
}
$results['H'] = @{ oldThrew = $oldThrew; oldErrId = $oldErr }

# ======================================================================
# Start-SvcAndWait（Phase 4.6-B）
#   事實來源 = SCM 實際狀態；NSSM exit code 僅為診斷；NSSM 文字輸出完全不採用。
#   逾時案例把 StartWaitSec 縮成 1 秒，避免測試真的等 40 秒。
# ======================================================================
$script:StartWaitSec = 1
$StartWaitSec = 1

# S_A. Stopped → start → StartPending → Running → PASS，且 start 只呼叫一次
#      查詢順序：① 初始判斷 ② start 後首次 ③④ 輪詢
Reset-Stub @('Stopped', 'Stopped', 'StartPending', 'Running')
$results['S_A'] = Invoke-StartCase

# S_B. Stopped → start → 一直 Stopped 到逾時 → throw（訊息含狀態 + exit code）
Reset-Stub @('Stopped') -NssmExit 3
$script:StatusQueue = @('Stopped') * 400
$script:NssmCalls = @()
$results['S_B'] = Invoke-StartCase

# S_C. LastNssmExit != 0，但 SCM 最終 Running → PASS + warning，不 throw
Reset-Stub @('Stopped', 'StartPending', 'Running') -NssmExit 5
$results['S_C'] = Invoke-StartCase

# S_D. Absent → throw，且**完全不呼叫** nssm start
Reset-Stub @('Absent')
$script:StatusQueue = @('Absent') * 10
$script:NssmCalls = @()
$results['S_D'] = Invoke-StartCase

# S_E. 初始就是 Running → idempotent success，且不呼叫 nssm start
Reset-Stub @('Running')
$script:StatusQueue = @('Running') * 10
$script:NssmCalls = @()
$results['S_E'] = Invoke-StartCase

# S_F. StartPending → Running：正常等待完成（初始為 StartPending 也要能收斂）
Reset-Stub @('StartPending', 'StartPending', 'Running')
$results['S_F'] = Invoke-StartCase

# S_G. StartPending 一路到逾時 → throw
Reset-Stub @('StartPending') -NssmExit 0
$script:StatusQueue = @('StartPending') * 400
$script:NssmCalls = @()
$results['S_G'] = Invoke-StartCase

# S_H. nssm exit=0 但 SCM 最終不是 Running → 仍必須 FAIL（不得只看 exit code）
Reset-Stub @('Stopped') -NssmExit 0
$script:StatusQueue = @('Stopped') * 400
$script:NssmCalls = @()
$results['S_H'] = Invoke-StartCase

Write-Output ("__P46CTL__" + ($results | ConvertTo-Json -Compress -Depth 6))
