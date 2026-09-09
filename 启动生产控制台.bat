@echo off
setlocal EnableExtensions
set "PROJECT_DIR=%~dp0"
set "URL=http://127.0.0.1:4173"
cd /d "%PROJECT_DIR%" || exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%URL%/api/dashboard' -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  start "" "%URL%"
  exit /b 0
)

python "%PROJECT_DIR%webapp\server.py"
pause
