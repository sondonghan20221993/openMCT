@echo off
REM ===========================================================================
REM start_openmct.bat
REM 스크립트 자기 위치(%~dp0) 기준 경로 - 어느 노트북/Windows 사용자든 동작.
REM (하드코딩 C:\Users\sdh97 제거)
REM ===========================================================================
setlocal
set "ROOT=%~dp0"

REM LoRa USB 시리얼 포트. 노트북마다 다르면 여기만 수정.
REM (장치 관리자 > 포트(COM & LPT) 에서 번호 확인)
if "%COMPORT%"=="" set "COMPORT=COM7"

REM 1) LoRa 브리지 (downlink WebSocket + uplink HTTP)
start "LoRa Bridge" cmd /k "cd /d "%ROOT%" && python fc_serial_ws_server.py --port %COMPORT% --baud 57600 --http-port 8082"

REM 2) OpenMCT UI (node_modules 없으면 자동 설치 후 vite dev)
start "OpenMCT UI" cmd /k "cd /d "%ROOT%my_openmct_app" && (if not exist node_modules npm install) && npm run dev"

timeout /t 5 /nobreak >nul
start http://localhost:5173
endlocal
