# Open MCT Bridge Notes

## 구현 현황 (2026-06-08, 필드/상태 갱신 2026-07-13)

Open MCT 앱 및 PC-side LoRa 브리지가 완성되어 동작 중이다.
단, **지상→기체 ACK 송신은 미구현** — §"링크 상태 갭" 참조.

## 데이터 흐름

```
드론 lora_tdm_app  (구 lora_fc_downlink_app — 삭제됨)
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
python fc_serial_ws_server.py --baud 57600 --http-port 8082
```

`--port` 기본값은 **`auto`** — LoRa USB(Silicon Labs CP210x, VID `0x10C4`)를 자동 탐지한다.
노트북마다 COM 번호가 달라도 고정 불필요. 탐지 실패/강제 시 `--port COM7` 명시.

### 다운링크 파서 (수신 → WS broadcast)

수신 포맷 (현행, `parse_lora_line()` 실구현 기준 — cfs-telemetry-app
`notes/lora_tdm_app_behavior_spec.md` §8과 필드 수 일치):

```
FC,<seq>,<ts_ms>,<roll>,<pitch>,<yaw>,<x>,<y>,<z>,<vx>,<vy>,<vz>,<lat_e7>,<lon_e7>,<alt_mm>,<fix>,<uplink_fb>,<sats>[,<rollspeed>,<pitchspeed>,<yawspeed>]
SH,<seq>,<ts_ms>,<health_state>,<fault_code>,<link_state>,<uplink_fb>
```

FC는 17필드 필수 + sats(1필드, 18필드 시 파싱, 2026-07-13 추가) +
rollspeed/pitchspeed/yawspeed 3필드 선택(idx 19~21, 22필드 시에만 파싱 — sats가
idx 17을 선점했으므로 rollspeed 그룹은 그 뒤로 밀림. rollspeed는 기체 인코더가
실제로 내보낸 적 없는 미구현 확장이며 idx만 예약돼 있음).
SH는 7필드. (구버전 서술이던 FC 16필드/SH 5필드는 틀렸음 — uplink_fb, link_state 등이
lora_tdm_app 대에 추가된 것을 이 문서가 못 따라갔던 것, 2026-07-13 정정)

단위 변환:
- `lat = lat_e7 / 1e7` (degrees)
- `lon = lon_e7 / 1e7` (degrees)
- `alt = alt_mm / 1000.0` (m)

WS broadcast JSON 필드:

| 필드 | 출처 | 비고 |
|------|------|------|
| `seq` | FC/SH seq | 소스별 독립 카운터 (§"링크 상태 갭" 참조 — heartbeat/packet_loss는 통합 처리) |
| `boot_ms` | ts_ms | FC 측 타임스탬프 |
| `roll/pitch/yaw` | FC | rad |
| `x/y/z` | FC | m |
| `vx/vy/vz` | FC | m/s |
| `lat/lon` | FC | deg (1e-7 변환) |
| `alt` | FC | m (mm 변환) |
| `fix` | FC | GPS fix type |
| `sats` | FC | 가시 위성 수 (SatellitesVisible, 2026-07-13 추가 — fix_type이 이진 게이트라면 sats는 품질 추세) |
| `uplink_fb` | FC/SH | 0=OK 1=CRC_FAIL 2=SEQ_FAIL |
| `link_state` | SH | lora_tdm_app 링크 상태 (지상 계산 아님, 기체 자체 판단) |
| `health_state` | SH | 0=NOMINAL 1=DEGRADED 2=RECOVERY |
| `fault_code` | SH | |
| `heartbeat` | 서버 | 누적 수신 패킷 수 (FC+SH) |
| `packet_loss` | 서버 | FC+SH 통합 seq gap 기반 손실률 (%) — **정답 설계**, 분리하면 안 됨(아래 §"packet_loss per-source 분리" 참조) |

## 링크 상태 갭 (2026-07-13)

**증상**: 본 서버는 다운링크 수신만으로 `[OK]`/`heartbeat`/`packet_loss`를 계산해
지상 화면에는 "정상"으로 보이지만, **지상→기체 `ACK,<seq>\n` 송신 코드가 없다.**

기체(`lora_tdm_app`)는 이 ACK를 keepalive로 사용해 `LinkState`를 CONNECTED로 전이시킨다
(`lora_tdm_app_behavior_spec.md` §11: `elapsed > LINK_TIMEOUT_MS(5000)` → DISCONNECTED).
즉 **지상 화면과 기체 판단이 서로 다른 링크 상태를 볼 수 있다** — 지상 "OK", 기체 "DISCONNECTED".

지금까지 실링크 시험은 사람이 수동으로 `ACK,<seq>\n`을 시리얼에 입력해 우회해왔다
(`cfs-telemetry-app/tests/TEST_CASES.md:481`). 상시 운용에는 쓸 수 없는 임시방편.

**해야 할 일**: `serial_reader()`가 다운링크 라인을 받으면(=파싱 성공 직후) 그 `seq`로
`ACK,<seq>\n`을 즉시 회신하도록 추가. v2(DL2) 전환 시에는 ACK2(바이너리) 회신으로 대체.

## 프로토콜 v2 (바이너리) — 계획, 미구현

다운링크 실효 갱신율을 0.77Hz→5Hz로 올리는 바이너리 프레임 설계가 확정되었다
(`cfs-telemetry-app/notes/lora_protocol_v2_spec.md`). 요지:

- DL2(0xD2, 46B) 통합 프레임 — FC/SH 필드를 하나로 합쳐 현재의 "FC/SH 슬롯 경합" 자체가 소멸
- UP2(0xB2) — hex 인코딩 폐지
- ACK2(0xA2, 5B) — CRC 포함 ACK, magic 바이트로 v1과 공존
- 본 서버의 `serial_reader()`는 `readline()` 기반(§코드) — v2는 종단문자가 없어 그대로는
  못 받는다. `cfs-telemetry-app/bridge/lora_downlink_decoder.py`의 `DownlinkStream`
  (바이트 스트림 상태머신, v1/v2 magic 분기)을 참고해 교체 필요.

**vendoring 상태(2026-07-13)**: 위 `DownlinkStream` 참조 구현을 본 리포에
`lora_protocol_v2.py`로 로컬 복사해둔 상태(파일은 디스크에 존재). 아직 git
커밋은 안 함 — auto-mode 권한 분류기가 "미승인 상태로 vendored 파일이
default 브랜치 커밋에 들어감"으로 차단, 명시적 사용자 승인 필요. 위
`readline()` → 상태머신 교체가 이 파일을 실제로 사용하기 시작하는 시점이므로
그 작업과 묶어서 커밋 여부를 재검토한다.

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

## 향후 구현사항 (planned)

### packet_loss per-source 분리 — 검토 결과 오판, 구현하지 않음 (2026-07-13 정정)

과거(commit `dee4221`)부터 "current bug"로 기록돼 있던 항목이었으나, `lora_tdm_app.c`
`RunTx()`를 직접 확인한 결과 **전제 자체가 틀렸다**:

> FC/SH는 "서로 독립된 seq 카운터"가 아니라, **단일 공유 카운터(`DownlinkSeq`)를
> 짝수=FC/홀수=SH로 교대**하는 설계다 (SB 파이프 포화 시 EKF_STATUS/SYSTEM_HEALTH
> 도착 순서 의존을 피하려는 의도적 설계, `lora_tdm_app.c:185` 주석 참조).

즉 지금의 `_update_link(seq)` 전역(통합) 계산이 **이미 정답**이다. 만약 여기 적힌
"해결책"대로 `_update_link(source, seq)`로 분리하면, 정상 링크에서도 같은 source
내 seq gap이 상시 2가 되어 **항상 ~50% 손실로 오판**한다 — 이건 본 파일 상단
`serial_reader()` 근처 주석(31~34행)이 이미 정확히 경고해온 바로 그 실수다.

**결론**: 통합 `packet_loss`(RF 링크 품질)는 현행 유지. FC 개별 상태를 보고 싶으면
`packet_loss`가 아니라 **FC boot_ms(TimestampMs) 정체 여부**를 봐야 한다 — FC UART가
끊겨도 lora_tdm은 캐시에 남은 마지막 값으로 FC 프레임을 계속 만들어 보내므로,
그 경우 loss는 0%인 채로 `boot_ms`만 멈춘다. 진단 도구:
`cfs-telemetry-app/tools/analyze_downlink_csv.py`(통합 손실률 + boot_ms 정체 감지로 분리).
