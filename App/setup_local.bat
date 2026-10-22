@echo off
rem One-time local setup: creates App\.venv and installs the App's and the agent's dependencies.
rem The API key is not handled here: the agent reads it from ..\Agent\.env (never committed).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment in App\.venv ...
    py -3.14 -m venv .venv || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Could not create the virtual environment. Install Python 3.12 or newer and try again.
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pytest
if errorlevel 1 (
    echo Installing the dependencies failed. If pip reports "no matching distribution", retry with:
    echo     .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt pytest
    exit /b 1
)

if not exist "..\Agent\.env" (
    echo.
    echo Note: ..\Agent\.env was not found, so the App will run in mock mode.
    echo For the real agent, copy ..\Agent\.env.example to ..\Agent\.env and set OPENROUTER_API_KEY.
)
echo.
echo Setup done. Start the App with run_local.bat
