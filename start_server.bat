@echo off
REM Smart Heating Controller - start the website on this Windows PC.
REM Double-click this file. The first run installs everything (needs internet).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up for the first time...
  python -m venv .venv
  if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
)
.venv\Scripts\python -c "import flask, requests, serial, waitress" 2>nul
if errorlevel 1 (
  echo Installing what the website needs - one time, about a minute...
  .venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt -r bridge\requirements.txt
)
echo.
echo Website: http://localhost:8000   (keep this window open)
echo The password is shown below the first time, and saved in data\password.txt
echo.
.venv\Scripts\python -m server.app
pause
