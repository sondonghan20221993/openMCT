@echo off
start "LoRa Bridge" cmd /k "cd /d C:\Users\sdh97\Documents\GitHub\openMCT && python fc_serial_ws_server.py --port COM7 --baud 57600 --http-port 8082"
start "OpenMCT UI" cmd /k "cd /d C:\Users\sdh97\Documents\GitHub\openMCT\my_openmct_app && npm run dev"
timeout /t 3 /noisy >nul
start http://localhost:5173
