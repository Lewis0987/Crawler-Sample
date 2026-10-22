@echo off
REM ============================================================
REM  Phase 6.10 ? Scheduled FIRST LIVE Attempt #2
REM  attempt_id : FIRST-LIVE-ATTEMPT2-20260922
REM  window     : 2026-09-22 00:05 ~ 00:30 (Asia/Taipei)
REM  [!] ONE-SHOT. No retry. No recurrence.
REM  [!] ?????????? PATH / cwd / VS Code / ??? stdin?
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_attempt2_20260922.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\FIRST-LIVE-ATTEMPT2-20260922.launcher.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
