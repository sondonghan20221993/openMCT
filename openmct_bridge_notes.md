# Open MCT Bridge Notes

## 구현 현황 (2026-06-08)

Open MCT 앱 및 PC-side LoRa 브리지가 완성되어 동작 중이다.

## 데이터 흐름

```
드론 lora_fc_downlink_app
  → LoRa RF
    → PC COM7 (Silicon Labs CP210x, 57600 baud)
      → fc_serial_ws_server.py
          ├─ WS  ws://127.0.0.1:8765   → Open MCT cfsRealtime plugin
          └─ HTTP http://127.0.0.1:8082 ← Open MCT uplinkCLI plugin

Open MCT (vite dev server: http://localhost:5173)
```

## fc_serial_ws_server.py

단일 프로세스로 직렬 포트를 공유하며 다운링크(WS) + 업링크(HTTP)를 동시에 처리한다.

```
python fc_serial_ws_server.py --port COM7 --baud 57600 --http-port 8082
```

### 다운링크 파서 (수신 → WS broadcast)

수신 포맷:

```
FC,<tx_count>,<ts_ms>,<roll>,<pitch>,<yaw>,<x>,<y>,<z>,<vx>,<vy>,<vz>,<lat_e7>,<lon_e7>,<alt_mm>,<fix_type>
SH,<tx_count>,<ts_ms>,<health_state>,<fault_code>
```

단위 변환:
- `lat = lat_e7 / 1e7` (degrees)
- `lon = lon_e7 / 1e7` (degrees)
- `alt = alt_mm / 1000.0` (m)

WS broadcast JSON 필드:

| 필드 | 출처 | 비고 |
|------|------|------|
| `seq` | tx_count | FC+SH 공유 카운터 |
| `boot_ms` | ts_ms | FC 측 타임스탬프 |
| `roll/pitch/yaw` | FC | rad |
| `x/y/z` | FC | m |
| `vx/vy/vz` | FC | m/s |
| `lat/lon` | FC | deg (1e-7 변환) |
| `alt` | FC | m (mm 변환) |
| `fix` | FC | GPS fix type |
| `health_state` | SH | 0=NOMINAL 1=DEGRADED 2=RECOVERY |
| `fault_code` | SH | |
| `heartbeat` | 서버 | 누적 수신 패킷 수 (FC+SH) |
| `packet_loss` | 서버 | FC+SH 통합 seq gap 기반 손실률 (%) |

### 업링크 HTTP (POST → LoRa TX)

```
GET  /health
GET  /api/uplink/meta
POST /api/uplink/config    {"scope": "cfs_core"|"mavlink_bridge", "param": str, "value": int}
POST /api/uplink/recovery  {"payload_hex": str (optional)}
```

LoRa ASCII 업링크 프레임 포맷:
```
UP,<version>,<class>,<seq>,<flags>,<payload_hex>,<crc16_hex>
```

## Open MCT 앱 (my_openmct_app)

```
npm run dev   # http://localhost:5173
```

### 텔레메트리 객체 트리

```
cFS FC Telemetry (root)
├─ Attitude:  roll, pitch, yaw
├─ Position:  x, y, z, vx, vy, vz
├─ GPS:       lat, lon, alt, sats, fix
├─ Status:    seq, boot_ms, flags, heartbeat, packet_loss, health_state, fault_code
└─ Uplink CLI (uplinkCLI plugin)
```

### CLI 명령어

```
uplinktest                              서버 연결 확인 및 파라미터 목록 출력
config <scope> <param> <value>          CONFIG 명령 전송 (LoRa)
recovery [payload_hex]                  RECOVERY 명령 전송 (LoRa)
help [config|recovery]
clear
```

cfs_core 파라미터: `attitude_timeout_ms`, `local_timeout_ms`, `gps_timeout_ms`, `ekf_timeout_ms`, `bridge_timeout_ms`, `publish_period_ms`

mavlink_bridge 파라미터: `attitude_interval_us`, `local_position_interval_us`, `global_position_interval_us`, `gps_raw_interval_us`, `ekf_status_interval_us`, `reconnect_interval_ms`, `heartbeat_interval_ms`

## 미지원 항목

- **RSSI / SNR**: COM7 LoRa 모듈이 투명 UART 모드로 동작하여 RSSI/SNR을 직렬로 출력하지 않음. 하드웨어 모드 변경 없이는 취득 불가.
- **sats**: LoRa FC 패킷에 위성 수(SatellitesVisible) 필드가 없어 미수신.

## 업링크 RF 충돌 → TDM 슬롯 정렬 송신 (해결)

### 문제
`lora_fc_downlink_app`이 FC/SH 패킷을 TX 중인 동안 PC에서 UP 프레임을 아무 때나 TX하면
동일 LoRa 채널에서 충돌 → 수신 프레임 깨짐 → `uplink_app` parse 실패.

```
EVS: UPLINK_APP: LoRa frame parse failed: UP1,1,10,...  ← 깨진 프레임
```

### 드론(Pi)측 설계 (반이중 TDM)
`lora_fc_downlink_app`은 downlink TX 직후 **300ms 동안만 RX 윈도우**를 열어 UP/HB를 읽는다.
즉 **지상국은 아무 때나 쏘면 안 되고, downlink를 받은 직후(=Pi RX 윈도우가 열린 순간) 그 슬롯에 UP를 보내야** 한다.
UP 프레임 경로: 지상 TX → Pi CP2102 RX 윈도우 → SB `UPLINK_RAW_MID`(0x1909) → `uplink_app` 파싱.

### 지상국측 해결 (fc_serial_ws_server.py)
별도 COM 포트 분리 대신, **단일 포트 + 슬롯 정렬 송신**으로 해결한다(드론 TDM 설계와 정합).

- HTTP 핸들러(`/api/uplink/*`)는 UP 프레임을 **즉시 전송하지 않고 `_pending_uplink` 큐에 적재**(`_queue_uplink`).
- `serial_reader()`가 downlink 라인을 수신한 직후(= Pi RX 윈도우 열림) `_flush_pending_uplink()`로 그 슬롯에 송신.
- SH 패킷이 FC 없이도 ~1Hz로 downlink되므로 슬롯은 항상 ~1초마다 열림 → uplink 지연 최대 ~1초.
- **자동 재전송(`_UPLINK_RETX`=4)**: 단발은 타이밍 지터/RF 손실로 한 슬롯을 자주 빗나간다(실측: 1번=무응답, 여러 번 붙여넣으면 적중). 동일 프레임을 연속 4개 슬롯에 재전송해 적중률을 높인다. `uplink_app`이 `IsSequenceAccepted`로 중복을 무시하므로 1발만 적용되고 나머지는 replay로 거부(무해) → 한 번의 명령으로도 안정 도달.

> 효과: downlink/uplink 충돌 없이 단일 LoRa 모듈로 양방향 동작. 별도 COM 포트(COM6) 불필요.

### 적용 대상
LoRa 양방향은 `fc_serial_ws_server.py`(다운링크 WS + 업링크 HTTP 통합, 최신)를 사용한다.
구버전 `lora_bridge.py` / `uplink_command_server.py` / `openmct_telemetry_server.py`는 잔재로 제거됨.
