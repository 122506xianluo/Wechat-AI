@echo off
setlocal
cd /d "%~dp0"
if not exist "data\bot.pid" (
  echo Bot is not running.
  exit /b 1
)
set /p BOTPID=<"data\bot.pid"
powershell -NoProfile -Command "$p=Get-Process -Id %BOTPID% -ErrorAction SilentlyContinue; if($p){Write-Host 'Bot is running. PID=%BOTPID%'; exit 0}else{Write-Host 'Bot is not running (stale PID file).'; exit 1}"
