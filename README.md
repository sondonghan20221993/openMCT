# CanSat Open MCT 텔레메트리 뷰어

UAV/캔위성 cFS 텔레메트리를 브라우저 대시보드로 실시간 시각화하는 시스템입니다.

## 구성 요소

| 파일 | 역할 |
|------|------|
| `my_openmct_app/` | Open MCT 웹 UI (Vite 기반) |
| `fc_serial_ws_server.py` | 직렬 포트(LoRa 수신기) → WebSocket 브리지 |
| `uplink_command_server.py` | HTTP → LoRa 명령 전송 서버 |

## 실행 방법

총 3개의 프로세스를 각각 터미널에서 실행합니다.

### 1. Open MCT UI

```powershell
cd my_openmct_app
npm install      # 최초 1회
npm run dev
```

브라우저에서 `http://localhost:5173` 접속

### 2. 텔레메트리 수신 브리지 (LoRa → WebSocket)

```powershell
python fc_serial_ws_server.py
```

- LoRa 수신기가 연결된 직렬 포트(기본 COM7, 57600 baud)에서 데이터 수신
- `ws://127.0.0.1:8765` WebSocket으로 Open MCT에 전달

### 3. 명령 전송 서버 (uplinkCLI 사용 시)

```powershell
python uplink_command_server.py --transport lora
```

- `http://127.0.0.1:8082`에서 HTTP 요청 수신
- Open MCT 내 Uplink CLI 터미널에서 명령 전송 가능

## 텔레메트리 구조

### Attitude
| 항목 | 단위 |
|------|------|
| Roll | rad |
| Pitch | rad |
| Yaw | rad |

### Position
| 항목 | 단위 |
|------|------|
| X, Y, Z | m |
| VX, VY, VZ | m/s |

### GPS
| 항목 | 단위 |
|------|------|
| Latitude | deg |
| Longitude | deg |
| Altitude | m |
| Satellites | — |
| GPS Fix | — |

### Status
| 항목 | 설명 |
|------|------|
| Sequence | 패킷 번호 (손실 감지용) |
| Boot Time | FC 부팅 후 경과 시간 (ms) |
| EKF Flags | EKF 상태 비트 |
| RSSI | 수신 신호 강도 (dBm) |
| SNR | 신호 대 잡음비 (dB) |
| Packet Loss | 패킷 손실률 (%) |
| Heartbeat | FC 생존 신호 |

## 직렬 입력 포맷

`fc_serial_ws_server.py`가 파싱하는 CSV 포맷:

```
FC,seq,boot_ms,roll,pitch,yaw,x,y,z,vx,vy,vz[,lat,lon,alt,sats,fix,flags]
GPS,seq,boot_ms,lat,lon,alt,sats[,fix]
EKF,seq,boot_ms,flags
```

## Uplink CLI 명령어

Open MCT 좌측 트리에서 **cFS FC Telemetry → Uplink CLI** 클릭 후 사용:

```
config <scope> <param> <value>   # CONFIG 명령 전송
recovery [payload_hex]           # RECOVERY 명령 전송
help                             # 도움말
clear                            # 터미널 초기화
```

**scope:** `cfs_core` | `mavlink_bridge`
