@echo off
setlocal
cd /d "%~dp0"

set "UV_CACHE_DIR=%~dp0.uv-cache"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    uv sync --frozen
    if errorlevel 1 exit /b 1
)

start "" /b "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launcher.py"
exit /b 0
