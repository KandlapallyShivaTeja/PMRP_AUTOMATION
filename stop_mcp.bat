@echo off
cd /d "%~dp0"
echo [INFO] Stopping any running MCP server processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mcp_server.py*' -and $_.Name -eq 'python.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo [INFO] MCP Server stopped successfully.
pause
