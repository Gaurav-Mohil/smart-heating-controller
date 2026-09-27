@echo off
REM Smart Heating Controller - put the website on the internet from this PC.
REM Double-click this file. Keep the window open; closing it takes the website offline.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up for the first time...
  python -m venv .venv
  if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt -r bridge\requirements.txt
)
.venv\Scripts\python run_public.py
pause
