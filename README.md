# Open MCT UAV 텔레메트리 뷰어

cFS 기반 UAV 텔레메트리를 브라우저 대시보드로 실시간 시각화하는 시스템입니다.  
LoRa 다운링크 수신, WebSocket 스트리밍, 업링크 명령 전송을 단일 프로세스로 처리합니다.

## 구성 요소

| 파일 | 역할 |
|------|------|
| `my_openmct_app/` | Open MCT 웹 UI (Vite 기반) |
| `fc_serial_ws_server.py` | LoRa 직렬 수신 + WebSocket 다운링크 + HTTP 업링크 (통합) |

> 구버전 분리/중복 서버 `uplink_command_server.py`, `lora_bridge.py`, `openmct_telemetry_server.py`는
> `fc_serial_ws_server.py`로 통합되어 **제거됨**.

## 실행 방법

터미널 2개로 실행합니다.

### 1. LoRa 브리지 (다운링크 + 업링크 통합)

```powershell
python fc_serial_ws_server.py --port COM7 --baud 57600 --http-port 8082
```

- `ws://127.0.0.1:8765` — Open MCT로 텔레메트리 broadcast
- `http://127.0.0.1:8082` — 업링크 명령 수신 (**TDM 슬롯 정렬**: 큐 적재 후 downlink 수신 슬롯에 송신)

### 2. Open MCT UI

```powershell
cd my_openmct_app
npm install      # 최초 1회
npm run dev
```

브라우저에서 `http://localhost:5173` 접속

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
| 항목 | 단위 | 비고 |
|------|------|------|
| Latitude | deg | lat_e7 / 1e7 변환 |
| Longitude | deg | lon_e7 / 1e7 변환 |
| Altitude | m | alt_mm / 1000 변환 |
| GPS Fix | — | fix_type |

> **Satellites 미지원**: LoRa FC 패킷에 위성 수 필드 없음

### Status
| 항목 | 설명 |
|------|------|
| Sequence | LoRa TX 카운터 (FC+SH 공유) |
| Boot Time | FC 부팅 후 경과 시간 (ms) |
| Packet Loss | FC+SH 통합 seq gap 기반 손실률 (%) |
| Heartbeat | 누적 수신 패킷 수 (FC+SH) |
| Health State | 0=NOMINAL 1=DEGRADED 2=RECOVERY |
| Fault Code | cfs_core_app 장애 코드 |

> **RSSI / SNR 미지원**: COM7 LoRa 모듈이 투명 UART 모드로 동작하여 수신 신호 강도를 직렬로 출력하지 않음

## LoRa 수신 포맷

`lora_fc_downlink_app`이 출력하는 ASCII CSV:

```
FC,<tx_count>,<ts_ms>,<roll>,<pitch>,<yaw>,<x>,<y>,<z>,<vx>,<vy>,<vz>,<lat_e7>,<lon_e7>,<alt_mm>,<fix_type>
SH,<tx_count>,<ts_ms>,<health_state>,<fault_code>
```

## Uplink CLI 명령어

Open MCT 좌측 트리에서 **cFS FC Telemetry → Uplink CLI** 클릭 후 사용:

```
uplinktest                              서버 연결 확인 및 파라미터 목록 출력
config <scope> <param> <value>          CONFIG 명령 전송 (LoRa)
recovery [payload_hex]                  RECOVERY 명령 전송 (LoRa)
help [config|recovery]                  도움말
clear                                   터미널 초기화
```

**scope:** `cfs_core` | `mavlink_bridge`

### 명령어 예시

```
> uplinktest
[OK] uplink server reachable  latency=3ms  transport=lora
     cfs_core params: attitude_timeout_ms, bridge_timeout_ms, ...
     mavlink_bridge params: attitude_interval_us, heartbeat_interval_ms, ...

> config cfs_core publish_period_ms 100
[OK] CONFIG accepted  seq=1  cfs_core.publish_period_ms=100

> config mavlink_bridge attitude_interval_us 50000
[OK] CONFIG accepted  seq=2  mavlink_bridge.attitude_interval_us=50000

> recovery
[OK] RECOVERY sent  seq=3

> config cfs_core publish_period_ms 999999999999
[ERR] value must be a uint32 integer (0 – 4294967295)

> config cfs_core bad_param 100
[ERR] unknown param 'bad_param'  available: attitude_timeout_ms, bridge_timeout_ms, ...
```

## 알려진 한계

- **업링크 RF 충돌 → TDM 슬롯 정렬로 해결**: 아무 때나 UP를 쏘면 드론의 반이중 RX 윈도우(downlink TX 후 300ms)를 놓쳐 충돌/유실됨. `fc_serial_ws_server.py`가 UP 프레임을 큐에 적재 후 downlink 라인 수신 직후 슬롯에 전송하도록 처리(별도 COM 포트 불필요). 상세: `openmct_bridge_notes.md`.
