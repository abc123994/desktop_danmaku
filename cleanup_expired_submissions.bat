@echo off
setlocal EnableExtensions

set "KEEP_HOURS=%~1"
if not defined KEEP_HOURS set "KEEP_HOURS=24"

echo 清理 main_v3 中超過 %KEEP_HOURS% 小時的投稿...
py -3.14 "%~dp0firebase_cleanup.py" --keep-hours "%KEEP_HOURS%" %~2
if errorlevel 1 (
    echo 清理失敗。
    pause
    exit /b 1
)

echo 清理完成。
pause
