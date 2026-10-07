@echo off
REM Scheduled TEJ update. Wrapper for Windows Task Scheduler.
REM Drives Excel, so the task must run in an interactive session:
REM   "Run only when user is logged on" -- a Session 0 task cannot start Excel.
pushd "%~dp0.."
set "TEJ_PYTHON=python"
if not exist "%TEJ_PYTHON%" (
  echo ERROR: Anaconda py312 not found at "%TEJ_PYTHON%"
  popd
  exit /b 2
)
"%TEJ_PYTHON%" scripts\update.py %*
set RC=%ERRORLEVEL%
popd
exit /b %RC%
