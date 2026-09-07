@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install

where py >nul 2>nul
if not errorlevel 1 (
  py -3.10 -m venv ".venv"
  if errorlevel 1 goto python_error
  goto install
)

where python >nul 2>nul
if errorlevel 1 goto python_error
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,10) else 1)"
if errorlevel 1 goto python_error
python -m venv ".venv"
if errorlevel 1 goto python_error

:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto install_error
if not exist ".env" copy /y ".env.example" ".env" >nul
if not exist "config.json" copy /y "config.example.json" "config.json" >nul
if not exist "data" mkdir "data"
echo Setup complete. Edit .env and config.json, then run start.bat.
exit /b 0

:python_error
echo Python 3.10 x64 was not found. Install Python 3.10.11 and retry.
exit /b 1

:install_error
echo Dependency installation failed.
exit /b 1
