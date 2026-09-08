@echo off
setlocal EnableExtensions
set "PROJECT_DIR=%~dp0"
set "RUNNER=%PROJECT_DIR%.agents\skills\cc-usr-claude-runner\scripts\run_tasks.py"
set "ENV_FILE=%PROJECT_DIR%.env"
set "DATABASE=%PROJECT_DIR%production.sqlite3"
cd /d "%PROJECT_DIR%" || exit /b 1

echo Claude Code User Satisfaction batch runner
echo.
python "%PROJECT_DIR%tools\batch_pipeline.py" --db "%DATABASE%" list
echo.
set /p "BATCH=Batch directory name (for example 0911): "
if not defined BATCH goto :cancel

echo.
python "%RUNNER%" --db "%DATABASE%" --batch "%BATCH%" --env-file "%ENV_FILE%" --list
if errorlevel 1 goto :failed

echo.
set /p "SELECTION=Question numbers, ranges, or task IDs (for example 1,3-5): "
if not defined SELECTION goto :cancel

echo.
python "%RUNNER%" --db "%DATABASE%" --batch "%BATCH%" --env-file "%ENV_FILE%" --select "%SELECTION%"
if errorlevel 1 goto :failed

echo.
set /p "CONFIRM=Type RUN to launch the selected questions: "
if /I not "%CONFIRM%"=="RUN" goto :cancel

python "%RUNNER%" --db "%DATABASE%" --batch "%BATCH%" --env-file "%ENV_FILE%" --select "%SELECTION%" --launch
if errorlevel 1 goto :failed
echo.
echo Batch launch completed.
pause
exit /b 0

:cancel
echo No questions were launched.
pause
exit /b 0

:failed
echo The operation failed. Check the message above.
pause
exit /b 1
