@echo off
setlocal enabledelayedexpansion

:: Change directory to the folder containing this batch script
cd /d "%~dp0"

echo [INFO] Stopping existing MCP server processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mcp_server.py*' -and $_.Name -eq 'python.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [INFO] Searching for virtual environment (looking for Scripts\activate.bat)...
set "VENV_PATH="

:: Loop through subdirectories to find Scripts\activate.bat
for /d %%d in (*) do (
    if exist "%%d\Scripts\activate.bat" (
        set "VENV_PATH=%%d"
        goto :venv_found
    )
)

:: Standard fallback checks
if exist ".venv\Scripts\activate.bat" (
    set "VENV_PATH=.venv"
    goto :venv_found
)
if exist "venv\Scripts\activate.bat" (
    set "VENV_PATH=venv"
    goto :venv_found
)

:venv_found
if "%VENV_PATH%"=="" (
    echo [ERROR] No Python virtual environment containing 'Scripts\activate.bat' was found in this folder.
    pause
    exit /b 1
)

echo [INFO] Found virtual environment at: %VENV_PATH%
echo [INFO] Activating virtual environment...
call %VENV_PATH%\Scripts\activate.bat

echo [INFO] Starting MCP server on port 8001 (Streamable-HTTP)...
echo [INFO] All logs will be written to mcp_server.log
python mcp_server.py > mcp_server.log 2>&1
