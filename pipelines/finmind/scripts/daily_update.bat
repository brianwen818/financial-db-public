@echo off
REM Windows Task Scheduler entry point for the daily incremental update.
REM Schedule: Mon-Sat 18:30 Taipei time (after the close and FinMind's publish).
REM
REM Set "Start in" to the finmind project directory when creating the task,
REM or rely on the pushd below.

pushd "%~dp0.."
".venv\Scripts\python.exe" -X utf8 "scripts\daily_update.py" %*
set RC=%ERRORLEVEL%
popd

if %RC% NEQ 0 (
    echo daily_update FAILED with exit code %RC%
    exit /b %RC%
)
exit /b 0
