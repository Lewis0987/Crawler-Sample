# C9 / B3 — 遠端 Guard 設計（設計稿）

> **狀態：DESIGN ONLY（2026-11-06）。未實作、未部署。**
>
> 🔴 本文件不構成部署授權。B3 的撰寫、部署、現場驗證**各自需要使用者另外明確授權**。
>
> 🔴 **不修改** `/home/etica/ems/auto_control.py`、`device_control_menu.py`、`device_control_operator.py`。
> B3 只替換 forced-command 腳本 `/home/etica/ems/phase6_remote_guard.sh` 的內容；
> `authorized_keys` 不變（`AUTHORIZED_KEYS_CHANGE_REQUIRED = False`）。
>
> 🔴 `FIRST_LIVE_HOLD = True`、Attempt #10 = HOLD。部署 B3 **不會**解除 HOLD，也不會核准 #3 / #4。

---

## 0. 現況（B2，2026-09-07 由使用者手動部署）

| 項目 | B2 現況（`phase6_remote_adapter.REMOTE_GUARD_B2_SH`） |
|---|---|
| 動詞 | `probe`、`loopcheck`、`status`、`pause`、`restore` |
| 控制動詞的前置檢查 | `require_single_screen`（`.auto` screen 恰好 1 個）＋ `require_identity`（`pgrep -f` 的第一個 PID：cwd = `/home/etica/ems`、cmd = `python3 auto_control.py`） |
| 注入方式 | `screen -S auto -X stuff 'stop\n'` / `'start\n'` / `'status\n'`（**未指定視窗、未清除輸入行**） |
| probe 輸出 | capability、`screen_count`、`pid`、`cwd`、`exe`、`cmd`、`ppid` |
| 本機 C7 | pid0 一致、exe 為 python3、ppid > 1 —— **只是必要條件** |

**B2 的缺口（B3 要處理的部分）：**

1. `pgrep -f 'auto_control\.py' | head -1`：多個實例時只檢查第一個；編輯器開著該檔也會被算入；
2. 沒有確認控制器在 `auto` screen 裡（可能在其他終端、nohup、其他 screen）；
3. 沒有確認控制器是該終端的前景程序（前景若是 `less`、`vim`，`stop\n` 會送到錯的程式）；
4. Attached 時仍會注入（有人正在畫面上打字時，輸入會混在一起）；
5. 未指定視窗、未處理殘留輸入。

---

## 1. 部署策略 P-6

| 選項 | 內容 | 優點 | 缺點 |
|---|---|---|---|
| **P-6a**（目前決定） | 不部署 B3；以**現場作業清單**（§7）暫代 | 不修改遠端 | 全靠人工；檢查與執行之間無自動把關 |
| **P-6b** | 部署 B3；**同時保留**現場作業清單 | 控制動詞在注入前自動檢查 §2～§3 | 修改遠端 forced command；需要新的測試組與本機解析 |
| P-6c | 部署 B3，但只啟用 probe 新欄位（控制動詞前置檢查以 `B3_ENFORCE=0` 關閉） | 先蒐集現場資料、不改變控制行為 | 兩階段部署，多一次遠端變更 |

**建議：** 維持 P-6a 直到 Attempt #10 前置條件都完成；若要部署，採 **P-6c → P-6b** 兩階段，
且**不論是否部署 B3，現場作業清單都必須保留**（§6 限制無法由 B3 消除）。

### 1.1 部署程序（沿用 `phase6_guard_deploy_plan.py` 的模式）

| 步驟 | 內容 |
|---|---|
| 0 | 前置：HOLD = True；沒有任何 Attempt 執行中；`auto_control.py` 狀態記錄（部署不影響它） |
| 1 | 備份現有 B2：`cp -p` 到 `phase6_guard_backup/phase6_remote_guard.B2.<ts>.sh`，記錄 sha256 |
| 2 | 上傳到 stage 檔 `.phase6_remote_guard.sh.new` |
| 3 | `sha256sum -c` 比對本機 staged B3 的 sha256 |
| 4 | `bash -n` 語法檢查 |
| 5 | `chown etica:etica`、`chmod 0700` |
| 6 | `mv -f` 原子替換（同一檔案系統） |
| 7 | 部署後驗證（§1.2） |

### 1.2 部署後驗證（**只用唯讀動詞**）

| 順序 | 動作 | 通過條件 |
|---|---|---|
| 1 | `probe` | `guard_variant=B3`；capability 齊全；B3 新欄位（§2.6）全部存在且格式正確 |
| 2 | 解析 probe | 新欄位與現場作業清單的人工觀察一致（實例數、screen 狀態） |
| 3 | `loopcheck` | 只做 hardcopy，不注入 |
| 4 | 任意非控制字串 | REFUSED（exit 42） |

🔴 部署後驗證**不得**使用 `pause`、`restore`。
🔴 **待決策：** B2 的部署後驗證曾使用 `status`。`status` 會把 `status\n` 注入控制器的 stdin，
雖然只讓它印出參數，但仍是輸入注入。B3 建議**移出部署後驗證**，並讓 `status` 套用與控制動詞相同的前置檢查。

---

## 2. PID / PPid / exe / screen 驗證

所有檢查只讀 `/proc` 與 `screen -ls`，以 `etica` 身分執行（screen socket 依使用者區分；
控制器若由其他帳號啟動，guard 看不到 → 判為不符，Fail Closed）。

### 2.1 控制器實例（取代 `pgrep -f | head -1`）

| 欄位 | 方法 | 規則 |
|---|---|---|
| `instances_strict` | 掃描 `/proc/[0-9]*/cmdline`（以 `\0` 分隔），argv **完全等於** `python3`、`auto_control.py`，且 `readlink -f /proc/P/cwd` = `/home/etica/ems` | 必須 = 1 |
| `instances_loose` | `pgrep -fc 'auto_control\.py'` | 只做資訊紀錄；**待決策：** > `instances_strict` 時是否拒絕（建議拒絕，安全方向的誤拒） |
| `pid` | strict 比對到的唯一 PID | 本機 C7 的 pid0 改用此值 |

### 2.2 exe

| 欄位 | 方法 | 規則 |
|---|---|---|
| `exe` | `readlink -f /proc/P/exe` | 必須等於現場記錄的 python3 實際路徑（例如 `/usr/bin/python3.x`，**現場證據 F-3**），不只比對開頭是 python3 |

### 2.3 PPid 鏈與 screen

| 步驟 | 方法 |
|---|---|
| 1 | `screen -ls` 取得 `<SPID>.auto` 那一行；必須恰好 1 行（沿用 `require_single_screen`） |
| 2 | 從 P 開始讀 `/proc/X/status` 的 `PPid:`，往上最多 8 層 |
| 3 | 途中遇到 `SPID` → `in_screen=1`；遇到 1、讀取失敗或超過 8 層 → `in_screen=0` |
| 4 | 輸出 `ppid_chain=P>…>SPID`（只做紀錄） |

### 2.4 視窗

| 欄位 | 方法 | 規則 |
|---|---|---|
| `window_count` | `SPID` 的直接子程序數（讀 `/proc/*/status` 的 PPid） | 必須 = 1 → 只有一個視窗，`-p 0` 沒有歧義 |
| 注入目標 | `screen -S auto -p 0 -X stuff ...` | 固定 0 號視窗 |

🔴 `screen -Q windows` 依版本而定，**不採用**；視窗與程序的對應改由「只有一個視窗」間接保證。

### 2.5 /proc/P/stat 解析

`comm` 欄位可能含空白或括號，**必須取最後一個 `)` 之後**的字串再分欄：

```
<pid> (<comm>) <state> <ppid> <pgrp> <session> <tty_nr> <tpgid> ...
               欄3     欄4    欄5    欄6        欄7      欄8
```

### 2.6 probe 新增欄位（B3）

```
guard_variant=B3
instances_strict=<n>
instances_loose=<n>
pid=<P>
exe=<path>
ppid_chain=<P>>...>SPID
in_screen=0|1
foreground=0|1
tty_nr=<n>
screen_state=Detached|Attached|Unknown
window_count=<n>
preconditions_ok=0|1
```

本機解析規則：任何欄位缺少、重複、格式不符 → 整體視為 **NOT_VERIFIED**（Fail Closed），
不得以預設值補齊。

---

## 3. Attached / Detached 與前景程序判定

| 判定 | 方法 | 規則 | 可靠度 |
|---|---|---|---|
| Detached | `screen -ls` 的 `<SPID>.auto` 行包含 `(Detached)` | 只有 `(Detached)` 才通過；`(Attached)`、`(Multi, attached)`、其他字樣 → `screen_state` 非 Detached → 拒絕 | 高；但檢查後才有人 attach 無法排除 |
| 前景程序 | `/proc/P/stat`：`pgrp == tpgid` 且 `tty_nr != 0` | 不成立 → 拒絕 | 高；**「前景」不等於「正在 `input()` 等輸入」** |
| 殘留輸入 | 注入字串改為 `$'\025stop\n'`（`\025` = ^U） | 只是降低風險 | **不能**當作防護：copy mode、`stty` 被改、殘字已送出時都無效 |

**控制動詞（`pause`、`restore`，以及 `status`）的前置檢查全部成立才注入：**

```
require_single_screen
instances_strict == 1  (以及待決策的 instances_loose 規則)
cwd / cmd / exe 符合
in_screen == 1
window_count == 1
foreground == 1
screen_state == Detached
```

任一不符 → 新增結束碼 `EX_PRECOND=46`，**不執行 `screen -X stuff`**，stderr 輸出未通過的項目。

---

## 4. Fail Closed

| 情境 | guard 行為 | Phase 6 端對應（既有語意，不放寬） |
|---|---|---|
| `pause` 前置檢查不符（46）／身分不符（45）／screen 歧義（44） | 不注入 | pause **未送出** → `NOT_SENT` → ABORTED，沒有成立恢復責任 |
| `pause` 注入逾時（41） | 結果不明 | `SEND_UNKNOWN` → `PAUSE_OUTCOME_UNKNOWN`，責任保留，不重送 |
| `restore` 前置檢查不符（44／45／46） | 不注入 | restore 未送出 → 責任保留（RESTORE_FAILED_CRITICAL / WITHHELD 類），C4 阻擋下一次 Attempt，需人工處理（C5） |
| `restore` 注入逾時（41） | 結果不明 | `RESTORE_OUTCOME_UNKNOWN`，責任保留，不重送 |
| `probe` 新欄位缺少或格式錯誤 | — | NOT_VERIFIED → 交接前把關不通過，pause 不送 |
| `/proc` 讀取失敗 | 該項判為不符 | 同上 |
| capability 回報非 B3（部署 B3 之後） | — | 本機 capability gate 拒絕 |

🔴 B3 拒絕 restore 時，external **仍維持暫停**、責任保留 —— 這是刻意的：
不確定目標時不注入，比注入到錯的程式安全。

⚠ **結束碼 46 與現行本機程式：** `phase6_remote_adapter` 目前把 41–45 以外的非零結束碼歸為
`SSH_ERROR` → `SEND_UNKNOWN`（第 1706、1744 行）。若 B3 先部署、本機未更新，46
會被記成「不確定是否已送出」：仍是 Fail Closed（責任保留、不重送），但會把「確定沒有注入」
誤記為不確定。因此：

- **選項 i：** 先更新本機（46 → 與 45 相同的「確定未注入」語意），再啟用 B3 的強制檢查（P-6c → P-6b）；
- **選項 ii：** B3 的前置檢查失敗沿用結束碼 45（不新增 46），本機不需改對應，但 stderr 需區分原因。

此為待決策事項（§10-5）。

---

## 5. 回滾程序（B3 → B2）

| 步驟 | 內容 |
|---|---|
| 觸發 | probe 失敗、新欄位解析失敗、身分不符、loopcheck 失敗、allowlist 非預期、現場判定 B3 誤拒過多 |
| 1 | `sha256sum` 確認備份檔等於部署前記錄的 B2 sha256 |
| 2 | `cp -p` 備份到 stage 檔 |
| 3 | `bash -n` |
| 4 | `chmod 0700` |
| 5 | `mv -f` 原子替換 |
| 6 | `ls -l` + `sha256sum` 確認 |
| 7 | 唯讀驗證：`probe` 的 `guard_variant=B2`、`loopcheck`；**不使用** pause / restore / status |
| 8 | 本機 `DEPLOYED_GUARD_VARIANT` 維持或改回 B2（由使用者修改並 commit） |

回滾期間 HOLD = True；回滾**不需要**動 `authorized_keys` 或 `auto_control.py`。

---

## 6. 無法證明的事項（必須保留）

| 項目 | 說明 |
|---|---|
| **唯一控制器** | B3 只能證明「這台主機、這個帳號、這個 cmdline 只有一個程序」。改過檔名的副本、其他帳號、其他主機、HMI 本身的排程、人工從 HMI 或 `device_control_menu.py` 操作，guard **都看不到** |
| copy mode | screen 指令沒有可靠的偵測方式；只靠「Detached 才注入」間接降低；detach 後 copy mode 是否仍在**未證實** |
| 前景 ≠ 等待輸入 | 歸零迴圈執行中時，輸入會暫存，稍後才處理 |
| 殘留輸入 | ^U 只在行緩衝模式、刪行鍵為 ^U 時有效 |
| 時間差（TOCTOU） | 檢查到注入之間有人 attach、程序被替換，無法排除 |
| guard 本身 | 只要有 shell 權限的人修改 guard，所有檢查都會失效；sha256 只能在檢查時刻證明 |

因此 **Phase 6 端的控制權判定（PauseVerifier、C6 時間戳凍結、C7、C8 推論）與現場作業清單都必須保留**，
B3 只是額外一層。

---

## 7. 現場作業清單（P-6a 必須；P-6b 仍保留）

執行 Attempt 前，由有 shell 權限的現場人員以唯讀方式確認並記錄：

| # | 項目 | 指令 / 方法 |
|---|---|---|
| L1 | 只有一個控制器實例 | `pgrep -fa 'auto_control\.py'` → 恰好 1 行 |
| L2 | 在 `auto` screen 內、只有一個視窗 | `ps -eo pid,ppid,pgid,tpgid,tty,stat,cmd --forest` |
| L3 | Detached | `screen -ls` |
| L4 | 前景是 python | L2 輸出中 python 的 `stat` 含 `+`（前景程序群組） |
| L5 | 沒有人登入在畫面上 | `who`；確認沒有 attach |
| L6 | 執行期間不 attach、不操作 HMI 控制、不開 `device_control_menu.py` | 現場人員書面確認 |
| L7 | 執行中參數未被修改 | 現場人員確認自啟動後沒有輸入 `ch`、`dc`、`max_kw` 等指令 |

---

## 8. 本機配合修改（只在 P-6b / P-6c 時，另外授權）

| 項目 | 內容 |
|---|---|
| `phase6_remote_adapter` | 新增 `REMOTE_GUARD_B3_SH`；`KNOWN_GUARD_VARIANTS` 加入 B3；新增 B3 欄位解析；B2 常數與測試**保留** |
| capability gate | 部署 B3 後要求 `guard_variant=B3` 且 `preconditions_ok=1`；不得只看本機 `DEPLOYED_GUARD_VARIANT` |
| provider `probe()` | 解析新欄位；C7 的 pid0 改用 `instances_strict` 對應的 PID |
| 測試 | 新 B3 測試組：腳本文字（無 eval、絕對路徑、`-p 0`、^U、Detached 規則、46 結束碼）、欄位解析的缺漏 / 重複 / 格式錯誤、回滾後 capability 回到 B2 |

---

## 9. 現場證據需求（全部唯讀）

| 代號 | 內容 | 用途 |
|---|---|---|
| F-1 | `screen -v` | 確認版本與 `-ls` 輸出格式 |
| F-2 | `screen -ls` 實際輸出（Detached / Attached 兩種狀態各一次，由現場人員自行 attach/detach，不由 guard） | 定義 `screen_state` 的比對字樣 |
| F-3 | `readlink -f /proc/<P>/exe` | 固定 exe 期望值 |
| F-4 | `ps --forest` 的 SCREEN → bash → python 鏈與視窗數 | 驗證 §2.3、§2.4 |
| F-5 | `cat /proc/<P>/stat` | 驗證 pgrp / tty_nr / tpgid 欄位位置 |
| F-6 | `stty -a -F /dev/pts/N`（只顯示） | 確認刪行鍵是否為 ^U |
| F-7 | `sha256sum /home/etica/ems/phase6_remote_guard.sh` | 確認目前為已知的 B2 |

---

## 10. 待決策事項

1. P-6：維持 P-6a，或採 P-6c → P-6b。
2. `instances_loose > instances_strict` 時是否拒絕（§2.1）。
3. `status` 是否套用控制動詞的前置檢查，並移出部署後驗證（§1.2）。
4. 是否採用 `\025`（^U）前綴（§3）—— 只能降低風險。
5. 前置檢查失敗的結束碼：新增 46（需先更新本機對應），或沿用 45（§4 選項 i / ii）。
