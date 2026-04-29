@echo off
REM ──────────────────────────────────────────────────────────────────────────
REM  Trade Signals Scanner — Windows installer
REM
REM  Handles the most common Windows Python pitfalls:
REM    • Microsoft Store stub (python.exe redirector) — explicitly rejected
REM    • py launcher preferred over `python` when available
REM    • winget install with --scope user (no admin prompt)
REM    • Doesn't trust PATH after winget install — searches known locations
REM ──────────────────────────────────────────────────────────────────────────

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ========================================================
echo   Trade Signals Scanner -- Windows Setup
echo ========================================================
echo.

set "PYTHON="

REM ── 1. Try the py launcher first (most reliable on Windows) ──────────────
where py >nul 2>nul
if !errorlevel! equ 0 (
    py -3 -c "import sys; print(sys.executable)" 1>"%TEMP%\pyfind.txt" 2>nul
    if !errorlevel! equ 0 (
        set /p PYTHON=<"%TEMP%\pyfind.txt"
        del "%TEMP%\pyfind.txt" >nul 2>nul
        echo  Python found via py launcher: !PYTHON!
        goto have_python
    )
)

REM ── 2. Try `python` but REJECT the Microsoft Store stub ──────────────────
where python >nul 2>nul
if !errorlevel! equ 0 (
    python -c "import sys; print(sys.executable)" 1>"%TEMP%\pyfind.txt" 2>nul
    if !errorlevel! equ 0 (
        set /p CANDIDATE=<"%TEMP%\pyfind.txt"
        del "%TEMP%\pyfind.txt" >nul 2>nul
        echo "!CANDIDATE!" | findstr /i "WindowsApps" >nul
        if !errorlevel! equ 0 (
            echo  Found "!CANDIDATE!" -- this is a Microsoft Store stub, ignoring.
        ) else (
            set "PYTHON=!CANDIDATE!"
            echo  Python found: !PYTHON!
            goto have_python
        )
    )
)

REM ── 3. No real Python -- install via winget ──────────────────────────────
echo  No working Python found. Installing Python 3.11 via winget...
where winget >nul 2>nul
if !errorlevel! neq 0 (
    echo.
    echo  ERROR: winget is not available on this PC.
    echo.
    echo  Please install Python 3.11 or newer manually:
    echo    https://www.python.org/downloads/
    echo  IMPORTANT: tick "Add python.exe to PATH" during install.
    echo  Then re-run install.bat.
    pause
    exit /b 1
)

winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements
if !errorlevel! neq 0 (
    echo.
    echo  winget install failed. Install Python manually from python.org and re-run.
    pause
    exit /b 1
)

REM PATH is NOT refreshed inside a running cmd.exe -- search known install paths
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%PROGRAMFILES%\Python311\python.exe"
    "%PROGRAMFILES%\Python312\python.exe"
) do (
    if exist %%P (
        set "PYTHON=%%~P"
        goto have_python
    )
)

echo.
echo  Python installed but its location was not auto-detected.
echo  Please CLOSE this window, open a NEW Command Prompt, and re-run install.bat.
pause
exit /b 1

:have_python

REM ── 4. Create virtual environment ────────────────────────────────────────
if exist ".venv\Scripts\python.exe" (
    echo  Virtual environment already exists at .venv\
) else (
    echo  Creating virtual environment...
    "!PYTHON!" -m venv .venv
    if !errorlevel! neq 0 (
        echo  venv creation failed using "!PYTHON!"
        pause
        exit /b 1
    )
    if not exist ".venv\Scripts\python.exe" (
        echo  venv reported success but .venv\Scripts\python.exe is missing.
        echo  Try installing Python manually from python.org and re-run.
        pause
        exit /b 1
    )
)

REM ── 5. Install dependencies ──────────────────────────────────────────────
echo.
echo  Installing dependencies (this can take a few minutes the first time)...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo  Dependency install failed. Check your internet connection and retry.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   Setup complete!
echo ========================================================
echo.
echo  To start the scanner, double-click run.bat
echo.
pause
