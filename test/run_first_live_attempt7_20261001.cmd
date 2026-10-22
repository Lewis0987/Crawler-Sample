@echo off
REM ============================================================
REM  Phase 6.10 - Scheduled FIRST LIVE Attempt #7
REM  attempt_id : FIRST-LIVE-ATTEMPT7-20261001
REM  window     : 2026-10-01 00:05 ~ 00:30 (Asia/Taipei)
REM  [!] ONE-SHOT. No retry. No recurrence. No Attempt #8.
REM  [!] HOST_POWERED_ON_REQUIRED = True (no catch-up, no wake).
REM  [!] Absolute paths only: no PATH / cwd / stdin dependency.
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_attempt7_20261001.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\FIRST-LIVE-ATTEMPT7-20261001.launcher.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
