@echo off
REM Smart Heating Controller - connect the USB Arduino Uno to the website.
REM Start the website first (start_server.bat), then double-click this file.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run start_server.bat once first to set things up.
  pause
  exit /b 1
)
.venv\Scripts\python bridge\serial_bridge.py %*
pause
