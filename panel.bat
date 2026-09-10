@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3.10 bootstrap.py
  exit /b %errorlevel%
)
where python >nul 2>nul
if not errorlevel 1 (
  python bootstrap.py
  exit /b %errorlevel%
)
echo Python 3.10 x64 was not found. Install Python 3.10.11 and retry.
pause
exit /b 1
