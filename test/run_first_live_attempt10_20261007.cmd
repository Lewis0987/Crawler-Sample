@echo off
REM ============================================================
REM  Phase 6.10 - Scheduled FIRST LIVE Attempt #10
REM  attempt_id : FIRST-LIVE-ATTEMPT10-20261007
REM  window     : 2026-10-07 00:05 ~ 00:30 (Asia/Taipei)
REM  [!] ONE-SHOT. No retry. No recurrence. No Attempt #11.
REM  [!] HOST_POWERED_ON_REQUIRED = True (no catch-up, no wake).
REM  [!] window_end = NO-NEW-RISK-ACTION DEADLINE (not a kill switch).
REM  [!] Absolute paths only: no PATH / cwd / stdin dependency.
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_attempt10_20261007.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\FIRST-LIVE-ATTEMPT10-20261007.launcher.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
