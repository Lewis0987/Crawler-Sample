<#
.SYNOPSIS
    Auto Monitor Service —— NSSM 安裝／管理腳本（Phase 4.6）

.DESCRIPTION
    把 auto_monitor_service.py 包裝成 Windows Service，讓智慧排程自動報告
    在無人登入、不開 Dashboard 的情況下持續運作。

    為什麼用 NSSM 而不是 sc.exe / srvany：
      Stop 時 NSSM 會先送 Ctrl+C 給子行程，正好觸發本專案既有的
      SIGINT -> stop_event -> report_pause() -> release_ownership() 流程。
      srvany 直接 TerminateProcess，會讓每次停止都留下 recording 的孤兒 Session。

    為什麼不用 pywin32：
      需額外安裝套件，且 Service 環境下 sys.stdout 為 None，需要自行處理
      400 餘處 print()。NSSM 的 AppStdout 直接給一個真實檔案 handle，零程式碼改動。

.PARAMETER Action
    install / start / stop / status / remove

.EXAMPLE
    # 以系統管理員身分執行 PowerShell
    .\install_service_nssm.ps1 -Action install
    .\install_service_nssm.ps1 -Action start
    .\install_service_nssm.ps1 -Action status
    .\install_service_nssm.ps1 -Action stop
    .\install_service_nssm.ps1 -Action remove

.NOTES
    install / start / stop / remove 需要系統管理員權限（status 不需要）。
    需先安裝 NSSM（https://nssm.cc/）並置於 PATH，或以 -NssmPath 指定。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'update', 'start', 'stop', 'status', 'remove')]
    [string]$Action,

    [string]$ServiceName = 'ESSAutoMonitor',

    # 留空＝自動解析為「與本腳本同目錄」的 nssm.exe（見下方 $ScriptDir 區塊）。
    # ⚠️ 不可在此寫 (Join-Path $PSScriptRoot 'nssm.exe')：以 -File 呼叫時，
    #    參數繫結早於 script scope 建立，$PSScriptRoot 為空字串會直接讓腳本失敗。
    [string]$NssmPath = '',
    [string]$PythonExe = 'C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe',
    [string]$ProjectRoot = 'D:\Crawler Sample',
    [double]$Interval = 10,
    [string]$DeviceHost = '192.168.128.110'
)

$ErrorActionPreference = 'Stop'

# 本腳本所在目錄。$PSScriptRoot 在主體可用；再以 MyInvocation 作為保險
# （某些呼叫方式下 $PSScriptRoot 可能為空）。
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

# NssmPath 未指定 → 預設同目錄的 nssm.exe（不必把 nssm 加進系統 PATH，
# 符合「不依賴使用者 PATH」的部署原則）。找不到時 Assert-Nssm 會再退回 PATH。
if (-not $NssmPath) { $NssmPath = Join-Path $ScriptDir 'nssm.exe' }

# ---- 路徑（一律絕對路徑：Service 以 LocalSystem 執行，cwd 為 System32，
#      不繼承使用者 PATH、不看得到對應磁碟）----
$TestDir     = Join-Path $ProjectRoot 'test'
$ScriptName  = 'auto_monitor_service.py'          # AppParameters 用（相對名，不含空白）
$ScriptPath  = Join-Path $TestDir $ScriptName     # 僅供存在性檢查與訊息顯示
$EnvFile     = Join-Path $TestDir 'login.env'
$LogDir      = Join-Path $ProjectRoot 'output\logs'
$StdoutLog   = Join-Path $LogDir 'auto_monitor_service.log'
$StderrLog   = Join-Path $LogDir 'auto_monitor_service.err.log'

# ---- Service 環境變數（Service 不繼承使用者環境）----
#   這是**唯一**的環境變數來源：Apply-ServiceSettings 用它寫入，
#   Assert-InstalledSettings 用它回讀比對。兩邊共用同一份，避免日後漂移。
#   API_ENV_FILE : 明確指定憑證檔，避免多個 *.env 造成探索順序不確定
#   NO_PROXY     : 設備為區域網路 IP，絕不可經 proxy
#   PYTHONIOENCODING / PYTHONUTF8：無 console 時仍確保 UTF-8 輸出
#   OPENBLAS_NUM_THREADS：openpyxl 會間接 import numpy，其 OpenBLAS 後端
#       會依 CPU 核心數建立 native thread pool。本產品不做任何 BLAS 運算
#       （openpyxl 只拿 numpy 做 isinstance 型別判斷），該 pool 是純負擔。
#       限制為 1 是 Phase 5.2 的資源最佳化，**不可移除**。
#       必須由 OS 在行程啟動前設定 —— numpy 一旦載入就來不及。
$EnvLines = @(
    "API_ENV_FILE=$EnvFile",
    "NO_PROXY=$DeviceHost,localhost,127.0.0.1",
    "no_proxy=$DeviceHost,localhost,127.0.0.1",
    "PYTHONIOENCODING=utf-8",
    "PYTHONUTF8=1",
    "PYTHONUNBUFFERED=1",
    "OPENBLAS_NUM_THREADS=1"
)

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "需要系統管理員權限。請以「以系統管理員身分執行」開啟 PowerShell 後重試。"
    }
}

function Assert-Nssm {
    <#
        解析順序：
          ① $NssmPath 直接指向存在的檔案（預設為 tools\nssm.exe，或使用者以參數指定）
          ② 退回 PATH 搜尋
          ③ 兩者皆無 → 明確錯誤並停止
        解析結果寫回 $script:NssmPath，之後 Invoke-Nssm 一律用絕對路徑呼叫。
    #>
    if (Test-Path -LiteralPath $NssmPath -PathType Leaf) {
        $script:NssmPath = (Resolve-Path -LiteralPath $NssmPath).Path
        return $script:NssmPath
    }
    $cmd = Get-Command $NssmPath -ErrorAction SilentlyContinue
    if ($cmd) {
        $script:NssmPath = $cmd.Source
        return $script:NssmPath
    }
    throw @"
找不到 NSSM。已嘗試：
  ① 同目錄：$(Join-Path $ScriptDir 'nssm.exe')
  ② PATH  ：$NssmPath
請自 https://nssm.cc/download 下載 nssm.exe（64-bit）放到 tools\ 目錄，
或以 -NssmPath 'C:\path\to\nssm.exe' 指定完整路徑。
"@
}

function Assert-Paths {
    foreach ($p in @($PythonExe, $ScriptPath)) {
        if (-not (Test-Path -LiteralPath $p)) { throw "找不到必要檔案：$p" }
    }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-Warning "找不到 $EnvFile —— Service 將無法登入設備。請確認憑證檔位置。"
    }
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        Write-Host "  已建立 log 目錄：$LogDir"
    }
}

$script:LastNssmExit = 0
$StopWaitSec = 40      # 等待服務真正停止的上限（AppStopMethodConsole 為 30s，留餘裕）
$StartWaitSec = 40     # 等待服務真正啟動的上限（刻意與 StopWaitSec 分開命名，兩者語意不同）

function Invoke-Nssm {
    <#
        呼叫 nssm 並回傳文字輸出；退出碼記於 $script:LastNssmExit。

        ⚠️ 為何要區域性改 $ErrorActionPreference：
           在 $ErrorActionPreference='Stop' 之下，原生 exe 寫到 stderr 的內容經 2>&1
           會被 PowerShell 轉成 ErrorRecord 並視為**終止性**錯誤而中斷整個腳本。
           nssm 對「服務已停止」「服務不存在」這類**可接受狀態**也會寫 stderr，
           於是 remove 流程在第一步 stop 就被誤判為 fatal 而中止（實測：RemoteException）。
           此處改以退出碼與實際服務狀態判定成敗，不讓 stderr 決定流程。
    #>
    param([string[]]$NssmArgs)
    $prev = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $out = & $NssmPath @NssmArgs 2>&1
        $script:LastNssmExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return (($out | ForEach-Object { $_.ToString() }) -join "`n").Trim()
}

function Get-SvcStatus {
    <#
        回傳服務狀態字串：Absent / Stopped / Running / StartPending / StopPending …

        刻意使用 PowerShell 原生 Get-Service 而非解析 nssm 的文字輸出：
        [Console]::OutputEncoding 為 big5，nssm 的錯誤訊息在重導向後會變成亂碼，
        用文字比對判斷狀態並不可靠。
    #>
    $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $s) { return 'Absent' }
    return $s.Status.ToString()
}

function Stop-SvcIfRunning {
    <#
        冪等停止。回傳 $true 表示「已確定為停止或不存在」，$false 表示停不下來。
        不以 nssm stop 的退出碼判定 —— 一律以實際服務狀態為準。
    #>
    $st = Get-SvcStatus
    if ($st -eq 'Absent')  { Write-Host '  服務不存在 → 略過 stop'; return $true }
    if ($st -eq 'Stopped') { Write-Host '  服務已為 Stopped → 略過 stop'; return $true }

    Write-Host "  服務目前為 $st → 送出 stop（NSSM 送 Ctrl+C，等 report_pause 完成）…"
    $null = Invoke-Nssm @('stop', $ServiceName)
    $deadline = (Get-Date).AddSeconds($StopWaitSec)
    while ((Get-Date) -lt $deadline) {
        $now = Get-SvcStatus
        if ($now -eq 'Stopped' -or $now -eq 'Absent') {
            Write-Host "  已停止（$now）"
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  ✗ 等待 $StopWaitSec 秒後仍為 $(Get-SvcStatus)"
    return $false
}

function Start-SvcAndWait {
    <#
        冪等啟動，並以 SCM 的實際狀態確認啟動成功。成功時回傳，失敗時 throw。

        判定原則（與 Stop-SvcIfRunning / Remove-SvcIdempotent 一致）：
          · SCM 實際狀態（Get-SvcStatus）＝**唯一**事實來源
          · NSSM exit code                ＝診斷資訊，會顯示，但不單獨決定成敗
          · NSSM 的文字輸出                ＝**完全不採用**，也不顯示

        ⚠️ 為何一個字都不顯示 NSSM 的原生輸出：
           實測（對不存在的服務下 status/get，取原始位元組）：
             stdout 為空；訊息一律走 stderr；
             b"Can't open service!\r\r\nOpenService(): ???????????????"
           那 15 個 '?' 就是位元組 0x3F 本身，NUL 位元組為 0 —— 代表中文訊息
           是在 nssm.exe **行程內部**做寬字元→窄字元轉換時被換成 '?' 的。
           資訊在那一刻就已毀損，PowerShell 端不論以 cp950 / utf-8 / utf-16 /
           latin-1 解碼都只會拿到同一串 '?'。
           因此：不改 code page、不用 chcp 65001、不嘗試重新解碼 —— 一律由本腳本
           自行輸出 Unicode 中文訊息。

        狀態分支：
          Absent            → throw（服務未安裝，不呼叫 nssm start）
          Running           → idempotent success（不呼叫 nssm start）
          其他（Stopped/StopPending/StartPending…）
                            → nssm start 後輪詢 SCM 直到 Running 或逾時
    #>
    $st = Get-SvcStatus
    if ($st -eq 'Absent') {
        throw "[NSSM] 服務未安裝：$ServiceName → 無法啟動（fail closed）。`n" +
              "       請先執行： .\install_service_nssm.ps1 -Action install"
    }
    if ($st -eq 'Running') {
        Write-Host "  服務已在執行（Running）→ 略過 start"
        return
    }

    Write-Host "  服務目前為 $st → 送出 start…"
    $null = Invoke-Nssm @('start', $ServiceName)   # 原生文字輸出刻意丟棄，不顯示、不解析
    $exit = $script:LastNssmExit

    $deadline = (Get-Date).AddSeconds($StartWaitSec)
    $now = Get-SvcStatus
    while ($now -ne 'Running' -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $now = Get-SvcStatus
    }

    if ($now -ne 'Running') {
        # fail closed：即使 nssm 回 0，只要 SCM 最終不是 Running 就是失敗。
        throw "[NSSM] 服務啟動失敗：$ServiceName 在 $StartWaitSec 秒內未進入 Running`n" +
              "       目前狀態：$now`n" +
              "       NSSM exit code：$exit`n" +
              "       請檢查 log：$StdoutLog"
    }

    if ($exit -ne 0) {
        # 不 silent ignore：非 0 保留為診斷訊息，但事實來源仍是 SCM。
        Write-Warning "NSSM 回報非 0（exit=$exit），但 SCM 實際狀態為 Running；以 SCM 狀態為準。"
    }
    Write-Host "  服務已啟動（Running）"
}

function Remove-SvcIdempotent {
    <#
        冪等移除：
          不存在 → 直接視為成功（already absent）
          Running → 先 stop 並確認停止，再 remove
          Stopped → 直接 remove
        停止失敗時**不得** remove（fail closed）；remove 後仍存在則明確報錯。
    #>
    if ((Get-SvcStatus) -eq 'Absent') {
        Write-Host "  服務不存在：$ServiceName（already absent，無需移除）"
        return
    }
    if (-not (Stop-SvcIfRunning)) {
        throw "[NSSM] 服務無法停止（目前狀態：$(Get-SvcStatus)）→ 不執行 remove（fail closed）。`n" +
              "       請先確認服務是否卡在 StopPending，或以服務管理員手動停止後再試。"
    }
    $out = Invoke-Nssm @('remove', $ServiceName, 'confirm')
    Start-Sleep -Milliseconds 500
    $after = Get-SvcStatus
    if ($after -ne 'Absent') {
        throw "[NSSM] remove 後服務仍存在（狀態：$after，nssm exit=$script:LastNssmExit）`n$out"
    }
    Write-Host "  已移除服務：$ServiceName"
}

function Remove-NulChars {
    <#
        移除字串中的 NUL（U+0000）。nssm 的輸出（尤其重導向時）可能夾帶 NUL。

        ⚠️ 不可寫成 .Replace([char]0, '')：
           String 有 Replace(Char,Char) 與 Replace(String,String) 兩個 overload，
           第一引數是 [char] 時 PowerShell 會綁定到 Replace(Char, Char)，
           此時第二引數必須是**單一字元**，空字串 '' 無法轉成 Char，
           直接拋 MethodArgumentConversionInvalidCastArgument。
           故以 [string][char]0 明確選用 Replace(String, String)。
           （實測：PS 5.1 與 PS 7 行為一致；.Trim([char]0) 只去頭尾，不可用。）
    #>
    param([AllowNull()][string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    return $Text.Replace([string][char]0, '')
}

function Get-NssmValue {
    param([string]$Key)
    # nssm get 的輸出可能夾帶 NUL 與尾端空白，統一清乾淨後比對
    return (Remove-NulChars (Invoke-Nssm @('get', $ServiceName, $Key))).Trim()
}

function Apply-ServiceSettings {
    <#
        install / update **共用**的唯一設定來源。

        為什麼要抽出來：install 與 update 若各寫一份 nssm set，日後一定漂移
        （改了 install 忘了改 update，服務更新後設定就不一致）。此處是唯一入口。

        本函式**不**呼叫 nssm install / nssm remove —— 服務的建立與移除分別由
        install 分支與 Remove-SvcIdempotent 負責，設定套用與生命週期嚴格分離。

        Application / AppParameters 也在此明確設定：nssm install 雖然會寫入它們，
        但 update 不跑 install，仍必須能把這兩項校正回腳本定義的值。
    #>
    # ---- 執行檔與參數 ----
    # ⚠️ AppParameters 一律使用**不含空白的相對腳本名**（原因見 install 分支註解）。
    Invoke-Nssm @('set', $ServiceName, 'Application', $PythonExe)         | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppParameters',
                  $ScriptName, '--interval', "$Interval")                 | Out-Null

    # ---- 基本設定 ----
    Invoke-Nssm @('set', $ServiceName, 'AppDirectory', $TestDir)          | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'DisplayName',
                  'ESS Auto Monitor Service')                            | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'Description',
                  '智慧排程自動建立充放電報告（無需開啟 Dashboard）')      | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START')     | Out-Null
    # LocalSystem：本機檔案全權、具 SeCreateGlobalPrivilege（Global\ Mutex 需要）
    Invoke-Nssm @('set', $ServiceName, 'ObjectName', 'LocalSystem')       | Out-Null

    # ---- 環境變數（定義見腳本頂部的 $EnvLines，此處只負責寫入）----
    Invoke-Nssm @('set', $ServiceName, 'AppEnvironmentExtra',
                  ($EnvLines -join "`r`n"))                               | Out-Null

    # ---- Log 與 rotation（Phase 4.6 的 (a) 層）----
    # ⚠️ 本專案有**兩套互相獨立**的 rotation，不要混為一談：
    #   (a) NSSM（此處）：AppRotateBytes 達 10MB 時把現有 log 改名為帶時間戳的檔案。
    #       **沒有「保留 N 份」的上限** —— 舊檔會一直累積，需自行或以排程清理。
    #   (b) auto_monitor_service.py 的 _rotate_log()：僅在「未經 NSSM 直接執行」
    #       或以 --log 指定時生效，採 .1~.5 輪替，固定保留 5 份。
    #   正式以 NSSM 部署時走 (a)；(b) 是不經 NSSM 時的第二層保障。
    Invoke-Nssm @('set', $ServiceName, 'AppStdout', $StdoutLog)           | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppStderr', $StderrLog)           | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppRotateFiles', '1')             | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppRotateOnline', '1')            | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppRotateBytes', '10485760')      | Out-Null   # 10MB
    Invoke-Nssm @('set', $ServiceName, 'AppStdoutCreationDisposition', '4') | Out-Null # append
    Invoke-Nssm @('set', $ServiceName, 'AppStderrCreationDisposition', '4') | Out-Null

    # ---- Stop：必須送 Ctrl+C，且給足 report_pause() 的時間 ----
    # report_pause() 內含 thread.join(SAMPLE_INTERVAL + 10) -> 最長約 15s。
    # 逾時設太短會在正常停止時被強制終止 -> 留下 recording 的孤兒 Session。
    Invoke-Nssm @('set', $ServiceName, 'AppStopMethodSkip', '0')          | Out-Null   # 不跳過任何階段
    Invoke-Nssm @('set', $ServiceName, 'AppStopMethodConsole', '30000')   | Out-Null   # Ctrl+C 等 30s
    Invoke-Nssm @('set', $ServiceName, 'AppStopMethodWindow', '5000')     | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppStopMethodThreads', '5000')    | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppKillProcessTree', '1')         | Out-Null

    # ---- Exit code：Phase 4.6 一律不自動重啟 ----
    #   0 正常結束 / 3 held_by_other（預期競爭，非故障）/ 4 ownership API 失敗
    #   自動重啟策略屬 Phase 4.7，此處刻意全部 Exit。
    Invoke-Nssm @('set', $ServiceName, 'AppExit', 'Default', 'Exit')      | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppExit', '0', 'Exit')            | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppExit', '3', 'Exit')            | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppExit', '4', 'Exit')            | Out-Null
    Invoke-Nssm @('set', $ServiceName, 'AppThrottle', '0')                | Out-Null
}


function Assert-InstalledSettings {
    <#
        安裝後回讀驗證：確認 NSSM 實際存進 Registry 的值與預期一致。

        重點是 AppParameters —— nssm install 會把參數經 argv 解析後以空白 join，
        含空白的絕對路徑會遺失引號，讓服務啟動時 Python 誤把 D:\Crawler 當成腳本。
        這種情況服務**必定**無法啟動，因此一律 throw，不以警告帶過。
    #>
    $expected = @{
        'Application'   = $PythonExe
        'AppDirectory'  = $TestDir
        'AppParameters' = "$ScriptName --interval $Interval"
    }
    Write-Host ""
    Write-Host "=== 安裝後回讀驗證 ==="
    $bad = @()
    foreach ($k in @('Application', 'AppDirectory', 'AppParameters')) {
        $actual = Get-NssmValue $k
        $want = $expected[$k]
        $ok = ($actual -eq $want)
        Write-Host ("  {0,-14}: {1}" -f $k, $actual)
        if (-not $ok) { $bad += [pscustomobject]@{ Key = $k; Actual = $actual; Want = $want } }
    }

    $ap = Get-NssmValue 'AppParameters'
    if ($ap -match '[A-Za-z]:\\' -or $ap -match '\\\\') {
        throw @"

[NSSM] AppParameters 驗證失敗
實際值：
$ap

預期值：
$ScriptName --interval $Interval

原因：
NSSM AppParameters 中的含空白絕對路徑可能遺失引號，
服務啟動時 Python 會將 D:\Crawler 誤認為腳本路徑，導致服務無法啟動。
請改用不含空白的相對腳本名（AppDirectory 已指向 $TestDir）。
"@
    }
    # ---- AppEnvironmentExtra ----
    # 這是 Phase 5.2 的資源最佳化設定所在，必須回讀確認，否則寫入失敗不會被發現。
    # ⚠️ 一律用**集合比較**：NSSM 回讀 REG_MULTI_SZ 不保證順序，硬綁順序會誤判。
    $envRaw = Get-NssmValue 'AppEnvironmentExtra'
    $envActual = @($envRaw -split "`r?`n" |
                   ForEach-Object { $_.Trim() } |
                   Where-Object { $_ -ne '' })
    Write-Host ("  {0,-14}: {1} 項" -f 'AppEnvExtra', $envActual.Count)
    foreach ($e in $envActual) { Write-Host "                  $e" }

    # ⚠️ 一律用**大小寫敏感**運算子（-cnotcontains / -clike）：
    #    本專案刻意同時設定 NO_PROXY 與 no_proxy（有些函式庫只讀小寫），
    #    預設的 -notcontains 不分大小寫，會把兩者視為同一項而放行錯誤設定。
    $envBad  = @()
    $missing = @($EnvLines  | Where-Object { $envActual -cnotcontains $_ })
    $extra   = @($envActual | Where-Object { $EnvLines  -cnotcontains $_ })
    $blas    = @($envActual | Where-Object { $_ -clike 'OPENBLAS_NUM_THREADS=*' })
    if ($envActual.Count -ne $EnvLines.Count) {
        $envBad += "項數為 $($envActual.Count)，預期 $($EnvLines.Count)"
    }
    if ($missing.Count -gt 0) { $envBad += "缺少：$($missing -join ' ｜ ')" }
    if ($extra.Count   -gt 0) { $envBad += "多出：$($extra   -join ' ｜ ')" }
    if ($blas.Count -ne 1) {
        $envBad += "OPENBLAS_NUM_THREADS 出現 $($blas.Count) 次（必須恰好 1 次）"
    } elseif ($blas[0] -cne 'OPENBLAS_NUM_THREADS=1') {
        $envBad += "OPENBLAS 值為 $($blas[0])，預期 OPENBLAS_NUM_THREADS=1"
    }
    if ($envBad.Count -gt 0) {
        $envMsg = ($envBad | ForEach-Object { "  - $_" }) -join "`n"
        $actMsg = ($envActual | ForEach-Object { "  $_" }) -join "`n"
        $expMsg = ($EnvLines  | ForEach-Object { "  $_" }) -join "`n"
        throw @"

[NSSM] AppEnvironmentExtra 驗證失敗（fail closed —— 不得繼續 start）
$envMsg

實際回讀（$($envActual.Count) 項）：
$actMsg

預期（$($EnvLines.Count) 項）：
$expMsg
"@
    }

    if ($bad.Count -gt 0) {
        $lines = ($bad | ForEach-Object { "  $($_.Key)`n    實際：$($_.Actual)`n    預期：$($_.Want)" }) -join "`n"
        throw @"

[NSSM] 安裝後設定驗證失敗（$($bad.Count) 項不符）
$lines
"@
    }
    Write-Host "  → 三項基本設定與 $($EnvLines.Count) 項環境變數均符合預期"
}

switch ($Action) {

    'install' {
        Assert-Admin; $nssm = Assert-Nssm; Assert-Paths
        Write-Host "使用 NSSM：$nssm"

        # ⚠️ AppParameters 一律使用**不含空白的相對腳本名**，不要用完整 ScriptPath。
        #    原因：nssm install 會把後續參數經 CRT 解析成 argv、再以空白 join 後存進
        #    Registry —— 包住含空白路徑的引號會在這個往返中遺失。結果變成
        #        D:\Crawler Sample\test\auto_monitor_service.py --interval 10
        #    啟動時 Python 收到 argv = ['D:\Crawler', 'Sample\test\...', ...]，
        #    直接報 can't open file 'D:\Crawler' 而啟動失敗。
        #    AppDirectory 已固定為腳本所在的 test 目錄，NSSM 會先切過去再啟動，
        #    因此相對名可正確解析，且完全不觸發引號問題。
        Invoke-Nssm @('install', $ServiceName, $PythonExe,
                      $ScriptName, '--interval', "$Interval") | Write-Host

        # 設定一律走 Apply-ServiceSettings（install / update 共用，避免兩份邏輯漂移）
        Apply-ServiceSettings

        # 全部 set 完成後才回讀驗證；任一項不符即 throw（服務已知無法啟動，不可放行）
        Assert-InstalledSettings

        Write-Host ""
        Write-Host "已安裝服務：$ServiceName"
        Write-Host "  執行            : $PythonExe $ScriptName --interval $Interval"
        Write-Host "                    （AppParameters 用相對名；完整路徑為 $ScriptPath）"
        Write-Host "  工作目錄        : $TestDir"
        Write-Host "  帳號            : LocalSystem"
        Write-Host "  API_ENV_FILE    : $EnvFile"
        Write-Host "  NO_PROXY        : $DeviceHost,localhost,127.0.0.1"
        Write-Host "  stdout / stderr : $StdoutLog"
        Write-Host "                    $StderrLog"
        Write-Host "  rotation        : NSSM 於 10 MB 時輪替（AppRotateFiles=1, AppRotateOnline=1）"
        Write-Host "                    註：NSSM 輪替不設保留份數上限，舊 log 會累積，需自行清理。"
        Write-Host "                    auto_monitor_service.py 的 .1~.5（保留 5 份）為另一套"
        Write-Host "                    獨立機制，僅在不經 NSSM 直接執行時生效。"
        Write-Host "  Stop            : Ctrl+C，逾時 30s"
        Write-Host "  Exit code       : 0/3/4 皆不自動重啟（Phase 4.6 規定）"
        Write-Host ""
        Write-Host "下一步： .\install_service_nssm.ps1 -Action start"
    }

    'update' {
        <#
            更新**既有**服務的設定，不建立、不移除服務。

            為什麼要有這個動作：
              對已存在的服務執行 -Action install 時，nssm install 會失敗，但因為
              Invoke-Nssm 刻意不 throw，後續 nssm set 仍會照跑 —— 設定確實會更新。
              那是**副作用**，不是設計契約，也沒有測試保證。Production 的設定更新
              不應依賴它，故獨立出 update 這條正式路徑。

            契約：
              · 服務不存在 → throw（fail closed），**不**偷偷退化成 install
              · 不呼叫 nssm install、不呼叫 nssm remove
              · 設定來源與 install 完全相同（Apply-ServiceSettings）
              · 回讀驗證不通過即 throw，不得繼續 start
              · 不改動 ServiceName 與各路徑（皆由腳本參數決定，與 install 同源）

            設定於服務執行中亦可寫入，但**要下次啟動才生效** ——
            正式流程為 stop → update → start。
        #>
        Assert-Admin; $nssm = Assert-Nssm; Assert-Paths
        Write-Host "使用 NSSM：$nssm"

        $st = Get-SvcStatus
        if ($st -eq 'Absent') {
            throw @"

[NSSM] 服務不存在：$ServiceName
update 只能更新**既有**服務，不會自動改為安裝（fail closed）。
若要首次安裝，請改用：
    .\install_service_nssm.ps1 -Action install
"@
        }
        Write-Host "更新既有服務：$ServiceName（目前狀態：$st）"
        if ($st -eq 'Running') {
            Write-Warning ("服務執行中 —— 設定會寫入，但要下次啟動才生效。" +
                           "正式流程建議：-Action stop → -Action update → -Action start")
        }

        Apply-ServiceSettings
        Assert-InstalledSettings

        Write-Host ""
        Write-Host "已更新服務設定：$ServiceName"
        Write-Host "  執行            : $PythonExe $ScriptName --interval $Interval"
        Write-Host "  工作目錄        : $TestDir"
        Write-Host "  環境變數        : $($EnvLines.Count) 項（含 OPENBLAS_NUM_THREADS=1）"
        Write-Host "  服務未被移除或重建，僅套用設定。"
        Write-Host ""
        Write-Host "下一步： .\install_service_nssm.ps1 -Action start"
    }

    'start' {
        Assert-Admin; Assert-Nssm | Out-Null
        # 成功與否一律由 Start-SvcAndWait 以 SCM 實際狀態判定並在失敗時 throw。
        # 不再有「固定 Start-Sleep 2 秒 + 只顯示表格不做判定」的偽成功路徑。
        Start-SvcAndWait
        Get-Service -Name $ServiceName | Format-Table -AutoSize | Out-String | Write-Host
        Write-Host "log： $StdoutLog"
    }

    'stop' {
        Assert-Admin; Assert-Nssm | Out-Null
        if (-not (Stop-SvcIfRunning)) {
            throw "[NSSM] 服務無法停止（目前狀態：$(Get-SvcStatus)）"
        }
        Get-Service -Name $ServiceName -ErrorAction SilentlyContinue |
            Format-Table -AutoSize | Out-String | Write-Host
        Write-Host "請確認 Session 已為 paused（非 recording）："
        Write-Host "  $ProjectRoot\output\charge_discharge_reports\<session>\session_state.json"
    }

    'status' {
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc) {
            Write-Host "服務未安裝：$ServiceName"
        } else {
            Write-Host "服務狀態：$($svc.Status)    啟動類型：$($svc.StartType)"
            $cmd = Get-Command $NssmPath -ErrorAction SilentlyContinue
            if ($cmd) {
                foreach ($k in @('Application', 'AppParameters', 'AppDirectory',
                                 'ObjectName', 'AppStdout', 'AppStderr',
                                 'AppRotateBytes', 'AppStopMethodConsole')) {
                    $v = (& $NssmPath get $ServiceName $k 2>&1 | Out-String).Trim()
                    Write-Host ("  {0,-22}: {1}" -f $k, $v)
                }
            }
        }
        Write-Host ""
        Write-Host "log 檔："
        foreach ($f in @($StdoutLog, $StderrLog)) {
            if (Test-Path -LiteralPath $f) {
                $fi = Get-Item -LiteralPath $f
                Write-Host ("  {0}  {1:N0} bytes  {2}" -f $fi.Name, $fi.Length, $fi.LastWriteTime)
            } else {
                Write-Host "  $([IO.Path]::GetFileName($f))  （尚未產生）"
            }
        }
        Write-Host ""
        Write-Host "Monitor Owner（僅供診斷；真正的互斥依據是 Windows Mutex）："
        $ownerFile = Join-Path $ProjectRoot 'output\charge_discharge_reports\monitor_owner.json'
        if (Test-Path -LiteralPath $ownerFile) {
            Get-Content -LiteralPath $ownerFile -Raw | Write-Host
        } else {
            Write-Host "  （無 monitor_owner.json —— 目前可能無人持有，或持有者未寫入）"
        }
    }

    'remove' {
        Assert-Admin; Assert-Nssm | Out-Null
        Remove-SvcIdempotent
        Write-Host "（log 檔保留於 $LogDir）"
    }
}
