@echo off
REM ──────────────────────────────────────────────────────────────────────────
REM  Trade Signals Scanner — Windows launcher
REM
REM  1. Checks for updates from GitHub (if update_config.json is configured)
REM  2. Starts the FastAPI server on http://localhost:8000
REM  3. Opens the scanner in your default browser
REM
REM  Close this window to stop the scanner.
REM ──────────────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

REM ── Verify install was run ────────────────────────────────────────────────
if not exist .venv\Scripts\python.exe (
    echo.
    echo  ERROR: No virtual environment found.
    echo  Run install.bat first to set up the scanner.
    echo.
    pause
    exit /b 1
)

REM ── Step 1: check for updates from GitHub ────────────────────────────────
echo.
echo ========================================================
echo   Checking for updates...
echo ========================================================
.venv\Scripts\python.exe updater.py
echo.

REM ── Step 2: open browser shortly after server starts ─────────────────────
start "" /b cmd /c "timeout /t 4 /nobreak >nul && start http://localhost:8000"

REM ── Step 3: start the server ─────────────────────────────────────────────
echo ========================================================
echo   Trade Signals Scanner -- running on http://localhost:8000
echo ========================================================
echo.
echo  Close this window to stop the scanner.
echo  The browser will open in 4 seconds.
echo.
.venv\Scripts\python.exe app.py
