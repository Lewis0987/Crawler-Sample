# 192.168.70.201 — Host Read-Only Investigation 指令單

> **給具 shell 權限者執行。2026-09-21。**
>
> 🔴 **全部唯讀。禁止 sudo 提權、start / stop / restart / kill / pkill /
> `screen -X stuff` / `systemctl start|restart` / crontab 修改 / 任何檔案修改 /
> 任何 PCS·BESS command。**
>
> 🔴 Claude 端**無 shell credential**：本機唯一金鑰 `phase6_firstlive_ed25519`
> 綁定 `command="/home/etica/ems/phase6_remote_guard.sh"`，
> 實測 `ssh … 'ps -ef'` → `REFUSED: only probe loopcheck status pause restore are allowed`（exit 42）。
> **不嘗試繞過 forced-command key。**

## 背景（Claude 端已取得的唯讀證據）

```
probe ×4        screen_count=1   guard_variant=B2   pid=''（空）
loopcheck ×4    exit=45  stdout: "IDENTITY: auto_control.py not running"
```
guard 內部：`controller_pid() { pgrep -f 'auto_control\.py' | head -1 || true; }`
→ 主機上 `pgrep -f 'auto_control\.py'` 回傳空。

🔴 同時間 HMI 觀測到 **PCS = CHARGING / +2.3 kW**（見文末）。
⚠️ **2026-09-21 使用者更正**：該筆 CHARGING 已由使用者說明為
**MANUAL OPERATOR ACTION**，`auto_control.py` 為 **MANUALLY STOPPED BY OPERATOR**。
因此**不再**以 `CONTROL_ORIGIN = UNRESOLVED` 描述該筆觀測。
本指令單的 A–F 僅在未來再次出現**無法解釋**的 controller 狀態時才需執行。

---

## A. BASIC HOST / SCREEN

```bash
date -Is
hostname
whoami

screen -ls

ps -eo pid,ppid,sid,pgid,user,lstart,etime,stat,cmd --forest
```

## B. CONTROLLER PROCESS DISCOVERY

```bash
pgrep -af 'auto_control\.py'
pgrep -af 'python'
pgrep -af '/home/etica/ems'

# 從完整 ps 結果搜尋關鍵字（唯讀）
ps -eo pid,ppid,lstart,etime,stat,user,cmd \
  | grep -Ei 'auto_control|bess_request|/home/etica/ems|python3|SCREEN' \
  | grep -v grep
```

找到 candidate PID 後（**只讀，不要操作它**），逐一執行：

```bash
PID=<candidate>

tr '\0' ' ' < /proc/$PID/cmdline ; echo
readlink -f /proc/$PID/cwd
readlink -f /proc/$PID/exe
cat /proc/$PID/status
ps -p $PID -o pid,ppid,lstart,etime,stat,user,cmd
```

> 若出現 `Permission denied` → 記錄 **PERMISSION_DENIED**，**不要升權**。

## C. SCREEN CHILD RELATIONSHIP

```bash
screen -ls
ps -eo pid,ppid,sid,user,lstart,etime,stat,cmd --forest | grep -A 10 -i screen
```

判定：screen session 之下的 child 是 `auto_control.py` / 其他 python / 只剩 shell / 無有效 controller child。

## D. DEPLOYED SOURCE

```bash
stat /home/etica/ems/auto_control.py
sha256sum /home/etica/ems/auto_control.py
ls -la /home/etica/ems/
```

期待既有 SHA256：
```
964bb642aa0e70a9663d636dc926150aaece60f13d28a745adbc53c41b3976ce
```
→ `SOURCE_HASH_MATCH` 或 `SOURCE_HASH_CHANGED`（若 changed，**不要自行解讀原因**）。

## E. AUTO-RESTART / LIFECYCLE SOURCE

```bash
systemctl --no-pager --type=service --all
systemctl --no-pager list-timers --all
systemctl --user --no-pager --type=service --all   2>/dev/null || true
systemctl --user --no-pager list-timers --all      2>/dev/null || true

crontab -l 2>/dev/null || echo "NO_USER_CRONTAB"

# 唯讀搜尋 unit / cron 定義
grep -RIl --exclude-dir=.git -e 'auto_control' -e '/home/etica/ems' \
  /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system \
  /home/etica/.config/systemd/user /etc/crontab /etc/cron.d 2>/dev/null || true

# 若上面有命中，逐檔顯示（唯讀）
# cat <hit-file>
```

→ `AUTO_RESTART_PATH_FOUND` / `NO_AUTO_RESTART_PATH_FOUND` / `AUTO_RESTART_PATH_UNRESOLVED`

## F. SOURCE / GUARD RELATIONSHIP

```bash
sha256sum /home/etica/ems/phase6_remote_guard.sh
grep -n 'controller_pid\|pgrep\|EXPECTED_CWD\|EXPECTED_CMD' \
  /home/etica/ems/phase6_remote_guard.sh
```

用途：確認 guard 目前的 pattern 仍為 `auto_control\.py`，
並與 B 段找到的真實 controller process 名稱／entry 比對。

> 🔴 只有在 **B 段證明 controller 其實存在、但 guard pattern 抓不到**時，
> 才可分類為 `GUARD_PID_DISCOVERY_DEFECT`。目前**不先下此結論**。

## G.（補充，非必要）控制來源線索

若上述 A–F 都找不到 controller，而 PCS 仍在充電，以下**唯讀**指令可協助釐清
是否有其他主機／程序在對 ESS HMI 下指令：

```bash
ss -tnp 2>/dev/null | grep -i ':80\|:8080\|:443' || true
last -n 20 2>/dev/null || true
ls -la /home/etica/ems/*.log 2>/dev/null || true
tail -n 50 /home/etica/ems/*.log 2>/dev/null || true
```

> 只讀 log 尾端；**不要**清除或輪替任何 log。

---

## 請回貼的內容

A~F（及 G，若執行）各段的**原始輸出**即可，不需整理。
Claude 收到後會完成 lifecycle 分類、auto-restart 分類與 control-origin 分類。
