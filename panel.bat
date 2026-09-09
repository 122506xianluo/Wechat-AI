@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import flask" 2>nul
if errorlevel 1 (
  echo Installing panel dependency...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Run setup.bat.
    pause
    exit /b 1
  )
)
echo Starting local control panel...
start "WeChat AI Panel" ".venv\Scripts\python.exe" "%CD%\app.py"
