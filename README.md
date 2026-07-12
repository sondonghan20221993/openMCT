# Open MCT UAV 텔레메트리 뷰어

cFS 기반 UAV 텔레메트리를 브라우저 대시보드로 실시간 시각화하는 시스템입니다.  
LoRa 다운링크 수신, WebSocket 스트리밍, 업링크 명령 전송을 단일 프로세스로 처리합니다.

## ⚠️ 설계 상태 (2026-07-13 현행화)

기체측은 `lora_fc_downlink_app`(삭제됨)에서 **`lora_tdm_app`으로 대체 완료** —
TDM 1000ms 주기 + RX 윈도우 300ms 구현되어 있음 (cfs-telemetry-app
`notes/lora_tdm_app_behavior_spec.md`). 아래 갭은 "미구현"이 아니라
지상(본 리포)이 기체 spec을 아직 못 따라간 상태.

- ✅ **지상국** (`fc_serial_ws_server.py`): TDM slot-aligned 업링크, v1 다운링크 파싱 구현 완료
- ❌ **지상 ACK 송신 없음**: 기체는 지상의 `ACK,<seq>\n`을 링크 keepalive로 요구하는데
  (behavior spec §11) 본 서버는 보내지 않음 → 기체 `LinkState`가 CONNECTED로 못 감.
  실링크 시험 때는 사람이 수동으로 `ACK,<seq>\n`을 주입해왔음 (임시방편, 상시 운용 불가).
- ⏳ **프로토콜 v2(바이너리) 미수신**: 다운링크 실효 갱신율을 0.77Hz→5Hz로 올리는
  바이너리 프레임(DL2/UP2/ACK2)이 설계 확정됨. 본 서버의 `readline()` 기반 수신 루프는
  종단문자 없는 바이너리를 못 받으므로 상태머신 교체 필요.

**단일 원본(wire format)**: `cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md`(v1),
`notes/lora_protocol_v2_spec.md`(v2). 본 문서의 프레임 포맷 서술은 참조일 뿐이며
불일치 시 위 spec이 우선한다 (cansat_2 `docs/04-repository-map.md` §3).

**다음 작업**: ① 지상 ACK 송신 추가 ② v2 수신(DL2)·ACK2 회신 통합.
(구 항목 "기체 lora_tdm_app 포팅"은 완료되어 제거됨 — 아래 `LORA_TDM_DESIGN_SPECIFICATION.md`는
구설계 이력 문서로 격하.)

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
python fc_serial_ws_server.py --baud 57600 --http-port 8082
```

- `--port` 기본값 **`auto`** — LoRa USB(Silicon Labs CP210x, VID 0x10C4)를 자동 탐지. 노트북마다 COM 번호가 달라도 동작.
- 자동 탐지 실패 또는 특정 포트 강제 시: `--port COM7` 처럼 명시.
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

**v1 (현행, `lora_tdm_app` 출력 ASCII CSV)**:

```
FC,<seq>,<ts_ms>,<roll>,<pitch>,<yaw>,<x>,<y>,<z>,<vx>,<vy>,<vz>,<lat_e7>,<lon_e7>,<alt_mm>,<fix>,<ufb>,<sats>
SH,<seq>,<ts_ms>,<state>,<fault>,<linkstate>,<ufb>
```

FC 18필드(2026-07-13: sats 추가, 하위호환 — 구17필드 프레임도 계속 파싱됨) / SH 7필드.
필드 상세 정의(단위·스케일)는
`cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md` §8 참조 — 본 문서는 요약일 뿐
단일 원본이 아니다.

`fc_serial_ws_server.py`는 지상→기체 `ACK,<seq>\n`을 **보내지 않는다** (위 상태 섹션 참조).

**v2 (바이너리, 설계 확정·구현 예정)**: DL2/UP2/ACK2 프레임.
`cfs-telemetry-app/notes/lora_protocol_v2_spec.md` 참조.

## Uplink 인터페이스

✅ **CLI** (텍스트 명령어) + **GUI** (폼 기반) 두 가지 제공

### Option 1: Uplink CLI (텍스트 명령어)

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

### Option 2: Uplink GUI (폼 기반)

**권장**: 사용자 친화적, 입력값 검증 자동

Open MCT 좌측 트리에서 **cFS FC Telemetry → Uplink GUI** 클릭 후 사용:

```
┌─────────────────────────────────────────┐
│ ● 연결됨   latency=5ms   transport=lora │
├─────────────────────────────────────────┤
│ [명령 선택] CONFIG▼  [Scope] cfs_core▼  │
│ [파라미터]  publish_period_ms▼          │
│ [값]        [________]  ms              │
│ [전송]  [RESET]  [RECOVERY]             │
├─────────────────────────────────────────┤
│ [OK] CONFIG accepted  seq=5              │
│ [RECOVERY] sent  seq=6                   │
└─────────────────────────────────────────┘
```

**특징**:
- ✅ 자동 param 목록 조회 (서버에서 실시간)
- ✅ 입력값 검증 (타입, 범위)
- ✅ 폴백: 서버 응답 없으면 기본값 사용
- ✅ 실행 결과 로그 표시
- ✅ CLI보다 복잡한 명령도 쉽게 구성

## 알려진 한계 & 미해결 이슈

### P0 (치명) — 포트 충돌
- **기체 LoRa 시리얼 포트 동시 접근**: `lora_fc_downlink_app`과 `uplink_app`이 동일 CP2102 포트를 동시에 열어 read 경쟁 발생. 
- **근본 원인**: lora_tdm_app의 TDM RX 윈도우 설계가 lora_fc_downlink_app 리네임 과정에서 버려짐.
- **해결**: `notes/LORA_TDM_DESIGN_SPECIFICATION.md` 따라 구현 (7~10h).
- **참고**: `cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md` (권위 명세).

### P1 (높음)
- **packet_loss per-source 분리**: FC/SH의 서로 다른 seq를 통합으로 계산하면 loss 왜곡됨. 분리 추적 필요.
- **업링크 RF 충돌**: TDM RX 윈도우 미구현 상태에서 RF 반이중 충돌 방어 불가.

### P2 (낮음)
- **RSSI / SNR 미지원**: LoRa 모듈이 투명 UART 모드 — 하드웨어 모드 변경 필요.
- **GPS sats 필드 미지원**: 패킷 포맷 확장 필요.

### 상세
- 미해결 이슈: `notes/solve_porting_py_to_c.md` §14.
- 설계 현황: `openmct_bridge_notes.md`.
