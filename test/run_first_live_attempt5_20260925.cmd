@echo off
REM ============================================================
REM  Phase 6.10 - Scheduled FIRST LIVE Attempt #5
REM  attempt_id : FIRST-LIVE-ATTEMPT5-20260925
REM  window     : 2026-09-25 00:05 ~ 00:30 (Asia/Taipei, OFF_PEAK)
REM  [!] ONE-SHOT. No retry. No recurrence. No Attempt #6.
REM  [!] Absolute paths only: no PATH / cwd / stdin dependency.
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_attempt5_20260925.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\FIRST-LIVE-ATTEMPT5-20260925.launcher.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
