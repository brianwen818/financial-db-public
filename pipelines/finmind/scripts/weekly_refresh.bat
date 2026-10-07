@echo off
REM Windows Task Scheduler entry point for the weekly adjusted-price refresh and reconciliation.
REM Schedule: Sunday 02:00 Taipei time.
REM
REM Set "Start in" to the finmind project directory when creating the task,
REM or rely on the pushd below.

pushd "%~dp0.."
".venv\Scripts\python.exe" -X utf8 "scripts\weekly_refresh.py" %*
set RC=%ERRORLEVEL%
popd

if %RC% NEQ 0 (
    echo weekly_refresh FAILED with exit code %RC%
    exit /b %RC%
)
exit /b 0
