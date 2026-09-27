@echo off
REM Smart Heating Controller - connect the USB Arduino Uno to the website.
REM Start the website first (start_server.bat), then double-click this file.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run start_server.bat once first to set things up.
  pause
  exit /b 1
)
set SERVER=http://localhost:8000
if exist data\device_token.txt (
  set /p KEY=<data\device_token.txt
) else (
  set /p KEY=Paste the device key from the website's Devices page: 
)
set /p SERVER_IN=Website address [%SERVER%]: 
if not "%SERVER_IN%"=="" set SERVER=%SERVER_IN%
.venv\Scripts\python bridge\serial_bridge.py --server %SERVER% --key %KEY%
pause
