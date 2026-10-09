@echo off
REM Double-click to open the Topstep Bot menu. First run installs everything automatically.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: installing Topstep Bot. This takes a minute...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Could not create a Python environment. Install Python 3.11 or newer from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during installation, then run this file again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 (
        echo Installation failed - see the messages above.
        pause
        exit /b 1
    )
)

REM One block: Windows reads a .bat file line by line while it runs, and an update may replace this file.
(
    ".venv\Scripts\topstep-bot.exe" %*
    echo.
    pause
    exit /b
)
