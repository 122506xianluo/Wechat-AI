@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run setup.bat first.
  exit /b 1
)
if not exist ".env" (
  echo Missing .env. Run setup.bat and fill in the LLM settings.
  exit /b 1
)
if exist "STOP" del /q "STOP"
".venv\Scripts\python.exe" bot.py --check
if errorlevel 1 exit /b 1
start "" /b ".venv\Scripts\pythonw.exe" "%CD%\bot.py"
timeout /t 2 /nobreak >nul
call status.bat
