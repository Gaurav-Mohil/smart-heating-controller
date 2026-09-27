@echo off
REM Smart Heating Controller - start the website on this Windows PC.
REM Double-click this file. The first run installs everything (needs internet).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up for the first time...
  py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt -r bridge\requirements.txt
)
echo.
echo Website: http://localhost:8000   (keep this window open)
echo The password is shown below the first time, and saved in data\password.txt
echo.
.venv\Scripts\python -m server.app
pause
