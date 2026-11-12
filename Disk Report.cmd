@echo off
rem Scans the C: drive and opens the report in your browser. Read-only: nothing is deleted.
rem Right-click > "Run as administrator" to also measure System Restore points.
title Disk report
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3 is needed to run the disk report: https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% "%~dp0disk_report.py" %*
if errorlevel 1 pause
