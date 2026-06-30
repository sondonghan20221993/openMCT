@echo off
REM ===========================================================================
REM start_openmct.bat
REM Script-relative paths (%~dp0) - works on any laptop / Windows user.
REM (hardcoded C:\Users\sdh97 removed)
REM ===========================================================================
setlocal
set "ROOT=%~dp0"

REM LoRa USB serial port. Default auto = CP210x (Silicon Labs) auto-detect.
REM If auto-detect fails or you must force a port, set COMx. (e.g. set COMPORT=COM7)
if "%COMPORT%"=="" set "COMPORT=auto"

REM 1) LoRa bridge (downlink WebSocket + uplink HTTP)
start "LoRa Bridge" cmd /k "cd /d "%ROOT%" && python fc_serial_ws_server.py --port %COMPORT% --baud 57600 --http-port 8082"

REM 2) OpenMCT UI (auto npm install if node_modules missing, then vite dev)
start "OpenMCT UI" cmd /k "cd /d "%ROOT%my_openmct_app" && (if not exist node_modules npm install) && npm run dev"

timeout /t 5 /nobreak >nul
start http://localhost:5173
endlocal
