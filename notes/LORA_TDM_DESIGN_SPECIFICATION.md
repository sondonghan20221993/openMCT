# LoRa TDM 설계 명세

**최종 수정**: 2026-07-09
**상태**: ⚠️ **구설계 이력 문서 — 현행 아님 (2026-07-13 격하)**

> 이 문서는 삭제된 구앱 `lora_fc_downlink_app` 기준으로 작성되었다. 기체는
> 이미 `lora_tdm_app`으로 대체 완료(cfs-telemetry-app `notes/lora_tdm_app_behavior_spec.md`,
> `notes/lora_protocol_v2_spec.md`)되어 있고, 본 문서의 프레임 필드 수·ACK 방향 서술은
> 현행과 다르다 (예: 본 문서는 ACK를 "기체→지상"이라 적은 곳과 "지상→기체"로 파싱하는
> 코드 스니펫이 같은 문서 안에 공존 — 실제로는 지상→기체가 맞다, README.md 참조).
> **이 문서의 프레임 포맷/시퀀스는 신뢰하지 말 것.** 아래는 TDM 개념 이해용 이력으로만 보존한다.

---

## 1. 설계 원칙 (구버전 서술 — 아래도 동일하게 낡음)

openMCT 시스템의 LoRa 통신은 **cfs-telemetry-app의 lora_tdm_app 동작 명세**를 따른다.
이 문서는 기체-지상국 간 인터페이스를 명시하며, 양쪽이 동일한 이해 하에 구현해야 한다.

참조:
- `cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md` (v1, 권위 문서)
- `cfs-telemetry-app/notes/lora_protocol_v2_spec.md` (v2, 권위 문서·설계 확정)
- 이 문서 (구버전 openMCT 구현 현황 — 참고용, 비권위)

---

## 2. 핵심 문제

### 이전 구조 (문제)
```
Pi lora_fc_downlink_app:    open(CP2102, O_RDWR)  — TX만
Windows uplink_command_server.py: (없음)
Pi uplink_app:               open(CP2102, O_RDONLY) — RX 직접 접근

→ 동일 포트를 두 cFS 앱이 동시 접근 → read 경쟁 → 바이트 유실
```

### 해결책 (TDM)
```
Pi lora_tdm_app (또는 lora_fc_downlink_app으로 리네임):
  ├─ serial port 독점 소유
  ├─ TX (1초당 1회, FC or SH 패킷)
  ├─ RX 윈도우 300ms (downlink TX 직후)
  │   ├─ ACK 수신 → 링크 상태 갱신
  │   └─ UP frame 수신 → CRC 검증 → UPLINK_APP_CMD_MID (0x18D0) SB 발행
  └─ 링크 상태 갱신 (CONNECTED / DEGRADED / DISCONNECTED)

Pi uplink_app:
  └─ SB 메시지만 구독 (시리얼 포트 닫음)

Windows fc_serial_ws_server.py:
  ├─ downlink RX (WS broadcast)
  └─ uplink TX (HTTP POST → _pending_uplink 큐 → downlink 후 flush)
```

---

## 3. TDM 주기 (시간 도메인)

```
0ms      FC TX (시퀀스 짝수)
    ↓
10ms     (TX 완료)
    ↓
10~310ms RX 윈도우 (300ms)
        ├─ ACK 수신 가능
        └─ UP frame 수신 가능
    ↓
310ms    (RX 윈도우 닫음)
    ↓
310~1000ms (idle)
    ↓
1000ms   SH TX (시퀀스 홀수)
    ↓
...
```

**설정값** (cfs-telemetry-app):
- `CYCLE_PERIOD_MS = 1000` (1초)
- `RX_WINDOW_MS = 300` (300ms)
- `LINK_LOSS_THRESHOLD = 3`
- `LINK_TIMEOUT_MS = 5000`

---

## 4. Downlink 패킷 형식

### FC State 패킷
```
FC,<seq>,<ts>,<roll>,<pitch>,<yaw>,<x>,<y>,<z>,<vx>,<vy>,<vz>,<lat_e7>,<lon_e7>,<alt_mm>,<fix>,<ufb>\n
```

| 필드 | 형식 | 단위/설명 |
|------|------|---------|
| `seq` | uint32 | LoRa TX 카운터 (전역) |
| `ts` | uint32 | FC 측 타임스탬프 (ms) |
| `roll`, `pitch`, `yaw` | float | rad |
| `x`, `y`, `z` | float | m (NED) |
| `vx`, `vy`, `vz` | float | m/s (NED) |
| `lat_e7` | int32 | 절대 위도 × 1e7 |
| `lon_e7` | int32 | 절대 경도 × 1e7 |
| `alt_mm` | int32 | 절대 고도 (mm) |
| `fix` | uint8 | GPS fix type |
| `ufb` | uint8 | Uplink feedback (0=OK, 1=CRC_FAIL, 2=SEQ_FAIL) |

### System Health 패킷
```
SH,<seq>,<ts>,<state>,<fault>,<linkstate>,<ufb>\n
```

| 필드 | 형식 | 설명 |
|------|------|------|
| `seq` | uint32 | LoRa TX 카운터 (FC와 공유) |
| `ts` | uint32 | SH 측 타임스탬프 (ms) |
| `state` | uint8 | cfs_core_app 헬스 상태 (0=NOMINAL, 1=DEGRADED, 2=RECOVERY) |
| `fault` | uint8 | 현재 fault code |
| `linkstate` | uint8 | LoRa 링크 상태 (0=DISCONNECTED, 1=CONNECTED, 2=DEGRADED) |
| `ufb` | uint8 | Uplink feedback |

---

## 5. Uplink 패킷 형식

### UP frame (기체 수신)
```
UP,<version>,<class>,<seq>,<flags>,<payload_hex>,<crc16_hex>\n
```

| 필드 | 설명 |
|------|------|
| `version` | Uplink 프로토콜 버전 (1) |
| `class` | 명령 클래스 (1=CONFIG, 2=ROUTE_UPDATE, 4=RECOVERY) |
| `seq` | 명령 시퀀스 (지상국이 번호 매김) |
| `flags` | 예약 (현재 0) |
| `payload_hex` | hex 인코딩 페이로드 (선택) |
| `crc16_hex` | CRC-16/CCITT-FALSE(`UP,...,payload_hex` 첫 6필드) |

### ACK frame (기체 송신 후)
```
ACK,<seq>\n
```
기체가 UP frame 수신 후 ACK를 즉시 반송. 지상국은 이를 RX 윈도우 종료 신호로 사용.

---

## 6. 구현 현황

### ✅ 지상국 (Windows, fc_serial_ws_server.py)

**완료**:
- ✅ 단일 프로세스, serial port 독점
- ✅ downlink RX → WS broadcast (JSON)
- ✅ uplink HTTP POST → _pending_uplink 큐 적재
- ✅ downlink 수신 직후 _flush_pending_uplink() → slot-aligned TX
- ✅ 자동 재전송 (_UPLINK_RETX=4)

**미구현**:
- packet_loss per-source 분리 (버그) — 현재 FC/SH 통합으로 계산되어 loss 왜곡. 해결: _update_link(source, seq) 분리 추적.

---

### ❌ 기체 (Pi, lora_fc_downlink_app)

**현재 상태**:
- ✅ FC/SH downlink TX 구현
- ❌ **RX 윈도우 미구현** (TDM 설계 핵심 누락)
- ❌ **UP frame 수신 미구현** (uplink_app에 위임)
- ❌ **uplink_app의 시리얼 포트 직접 오픈** (설계 원칙 위반)
- ❌ **포트 충돌** (두 앱이 동시 접근)

**필요 작업**:

| 항목 | 파일 | 작업 |
|------|------|------|
| RX 윈도우 300ms | `lora_fc_downlink_app.c` / `.h` | `RunRxWindow(300)` 추가 (lora_tdm_app에서 참고) |
| UP frame 수신+파싱 | `lora_fc_downlink_app_utils.c` | `ProcessRxLine()` 구현 (UP/ACK 분기) |
| UP → SB forward | `lora_fc_downlink_app.c` | `UPLINK_APP_CMD_MID (0x18D0)` publish |
| uplink_app 수정 | `uplink_app/fsw/src/uplink_app.c` | serial port 열기 제거, SB 구독만 |
| 링크 상태 관리 | `lora_fc_downlink_app.c` | `UpdateLinkState()` (CONNECTED/DEGRADED/DISCONNECTED) |

---

## 7. 구현 가능성 평가

### 위험도: **낮음** ✅

**이유**:
1. **이미 구현된 설계**: lora_tdm_app이 cfs-telemetry-app에서 완전히 명세된 상태
2. **참고 코드 존재**: `lora_tdm_app/fsw/src/lora_tdm_app.c/utils.c` — 1:1 복사 가능
3. **지상국 완성**: fc_serial_ws_server.py가 이미 TDM slot-aligned uplink 구현됨
4. **데이터 구조 일치**: FC/SH 캐시, UplinkFwdCmd 구조 기존 정의됨

### 예상 작업량

| 작업 | 파일 | 난이도 | 시간 |
|------|------|--------|------|
| RX 윈도우 구현 | lora_fc_downlink_app.c | 낮음 | 1~2h |
| UP frame 파싱 | lora_fc_downlink_app_utils.c | 중간 | 2~3h |
| uplink_app 수정 | uplink_app.c | 낮음 | 1h |
| 테스트 | 모두 | 중간 | 3~4h |
| **합계** | | | **7~10h** |

---

## 8. 단계별 구현 계획

### Phase 1: RX 윈도우 복원 (2h)
```c
// lora_fc_downlink_app.c: RunCycle() 후 
RunTx();              // ✅ 기존
RunRxWindow(300);     // ❌ 추가
UpdateLinkState();    // ❌ 추가
```

### Phase 2: UP frame 수신+파싱 (3h)
```c
// lora_fc_downlink_app_utils.c: ProcessRxLine()
if (strncmp(line, "UP,", 3) == 0)
    ProcessUpFrame(line);  // CRC 검증 + SB forward
else if (strncmp(line, "ACK,", 4) == 0)
    ParseAckFrame(line);   // 링크 상태 갱신
```

### Phase 3: uplink_app 수정 (1h)
```c
// uplink_app/fsw/src/uplink_app.c
// - OpenSerial() 호출 제거
// - UPLINK_APP_CMD_MID 구독 추가 (lora_fc_downlink_app이 forward)
```

### Phase 4: 통합 테스트 (4h)
- 기체-지상국 양방향 통신 확인
- 포트 충돌 해결 확인
- 링크 상태 전이 검증

---

## 9. 체크리스트

구현 시 확인 사항:

- [ ] `lora_tdm_app_behavior_spec.md` 모두 읽음
- [ ] lora_tdm_app 소스코드 구조 파악 완료
- [ ] RunRxWindow() 구현 및 테스트
- [ ] ProcessRxLine(UP frame) 구현 및 테스트
- [ ] UPLINK_APP_CMD_MID (0x18D0) forward 동작 확인
- [ ] uplink_app의 serial port 제거 및 SB 구독 추가
- [ ] Pi 런타임에서 포트 충돌 EVS 로그 제거 확인
- [ ] FC/SH 패킷 OpenMCT에서 정상 수신 확인
- [ ] 업링크 명령 정상 도달 확인 (uplink feedback byte)

---

## 10. 참고 링크

| 문서 | 위치 | 용도 |
|------|------|------|
| lora_tdm_app 명세 | `cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md` | 권위 설계 |
| lora_tdm_app 코드 | `cfs-telemetry-app/lora_tdm_app/fsw/src/` | 참고 구현 |
| openMCT bridge | `openMCT/fc_serial_ws_server.py` | 지상국 현황 |
| 미해결 이슈 | `openMCT/notes/solve_porting_py_to_c.md` §14 | 배경 |

---

## 11. 미해결 문제 추적

### P0 (치명)
- [x] uplink_app 포트 충돌 — **TDM 구현으로 해결됨**

### P1 (높음)
- [ ] TDM RX 윈도우 300ms 복원 필요
- [ ] packet_loss per-source 분리 (지상국, fc_serial_ws_server.py)

### P2 (중간)
- [ ] RSSI/SNR 미지원 (하드웨어 모드)
- [ ] GPS sats 필드 (패킷 확장)

---

**최종 결론**: TDM 설계는 명확하고, 구현 참고 코드가 완전히 존재합니다. 
기체 쪽 7~10시간 작업으로 포트 충돌을 완전히 해결할 수 있습니다.
