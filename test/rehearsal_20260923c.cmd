@echo off
REM ============================================================
REM  Phase 6.10 - Scheduled FIRST LIVE  VALIDATE-ONLY REHEARSAL
REM  id : VALIDATE-ONLY-REHEARSAL-20260923C
REM  [!] NOT a field authorization. --validate-only => no field I/O.
REM ============================================================
set "PY=C:\Users\Water\AppData\Local\Programs\Python\Python314\python.exe"
set "LAUNCHER=D:\Crawler Sample\test\run_scheduled_first_live.py"
set "CFG=D:\Crawler Sample\test\scheduled_first_live_rehearsal_20260923c.json"
set "LOG=D:\Crawler Sample\output\phase6_scheduled_first_live\rehearsal_20260923c_task.log"
set "PYTHONIOENCODING=utf-8"
"%PY%" "%LAUNCHER%" --config "%CFG%" --validate-only > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
