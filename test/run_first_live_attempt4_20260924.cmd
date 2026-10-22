@echo off
REM ============================================================
REM  Phase 6.10 - Scheduled FIRST LIVE Attempt #4
REM  attempt_id : FIRST-LIVE-ATTEMPT4-20260924
REM  window     : 2026-09-24 00:05 ~ 00:30 (Asia/Taipei)
REM  [!] ONE-SHOT. No retry. No recurrence. No Attempt #5.
REM  [!] Absolute paths only: no PATH / cwd / stdin dependency.
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_attempt4_20260924.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\FIRST-LIVE-ATTEMPT4-20260924.launcher.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
