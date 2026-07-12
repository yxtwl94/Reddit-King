@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "UV_CACHE_DIR=%~dp0.uv-cache"

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv was not found in PATH.
    echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
    exit /b 1
)

echo [1/2] Installing locked build and runtime dependencies...
uv sync --frozen
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)

echo [2/2] Building dist\Reddit-King.exe...
"%~dp0.venv\Scripts\pyinstaller.exe" ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "Reddit-King" ^
    --distpath "%~dp0dist" ^
    --workpath "%~dp0build" ^
    --specpath "%~dp0build" ^
    --collect-all bs4 ^
    --collect-all certifi ^
    "%~dp0launcher.py"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

if not exist "%~dp0dist\Reddit-King.exe" (
    echo [ERROR] Build finished but the EXE was not found.
    exit /b 1
)

echo.
echo [OK] Built: %~dp0dist\Reddit-King.exe
exit /b 0
