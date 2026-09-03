@echo off
setlocal EnableExtensions

chcp 65001 >nul

rem ============================================================
rem Firebase 即時桌面彈幕：Windows / Python 3.14.6
rem ============================================================

set "APP=%~dp0desktop_danmaku.py"
set "REQUIRED_VERSION=3.14.6"

if not exist "%APP%" (
    echo 找不到程式：
    echo     %APP%
    pause
    exit /b 1
)

echo.
echo 正在結束同一資料夾中舊的桌面彈幕程序...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$targets = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $PID -and $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and ($_.CommandLine -like '*desktop_danmaku.py*' -or $_.CommandLine -like '*desktop_danmaku.py*') }; foreach ($p in $targets) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host ('已結束 PID ' + $p.ProcessId) } catch { Write-Host ('無法結束 PID ' + $p.ProcessId + ': ' + $_.Exception.Message) } }"
timeout /t 1 /nobreak >nul

py -3.14 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,14) and sys.version_info[:3] >= (3,14,6) else 1)" >nul 2>nul
if errorlevel 1 (
    echo 找不到 Python %REQUIRED_VERSION% 或更新的 Python 3.14 維護版本。
    py -0p
    pause
    exit /b 1
)

for /f "delims=" %%V in ('py -3.14 -c "import platform; print(platform.python_version())"') do set "PY_VERSION=%%V"
echo 使用 Python %PY_VERSION%

py -3.14 -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo 正在安裝 PySide6...
    py -3.14 -m pip install --upgrade PySide6
    if errorlevel 1 (
        echo PySide6 安裝失敗。
        pause
        exit /b 1
    )
)

set "PYTHONW="
for /f "delims=" %%P in ('py -3.14 -c "import pathlib, sys; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))" 2^>nul') do set "PYTHONW=%%P"
if not defined PYTHONW (
    echo 無法解析 pythonw.exe 路徑。
    pause
    exit /b 1
)
if not exist "%PYTHONW%" (
    echo 找不到：%PYTHONW%
    pause
    exit /b 1
)

start "" /D "%~dp0" "%PYTHONW%" "%APP%"
if errorlevel 1 (
    echo 啟動失敗。
    pause
    exit /b 1
)

echo 已啟動 Firebase 即時桌面彈幕。
exit /b 0
