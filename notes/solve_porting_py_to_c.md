# 문제 노트: MAVLink 파싱 Python → C 이식 후 OpenMCT 텔레메트리 미수신

작성일: 2026-06-14

---

## 1. 문제 정의

### 증상
OpenMCT 대시보드에서 **heartbeat, health_state(status)만 표시**, attitude/position/GPS 텔레메트리 미표시.

### 이전 동작 (Python 파싱)
- `fc_serial_ws_server.py`에서 Python `pymavlink` 라이브러리로 MAVLink 파싱
- Attitude, GPS, Position 정상 표시

### 현재 상태 (C 파싱)
- `mavlink_bridge_app` (cFS 앱)이 C로 MAVLink 파싱 → cFS SB 발행
- `lora_tdm_app`이 SB 구독 → LoRa 직렬 포트로 ASCII CSV 전송
- `fc_serial_ws_server.py`가 ASCII CSV 파싱 → WebSocket → OpenMCT
- **결과: FC 패킷이 OpenMCT에서 표시 안 됨**

---

## 2. 데이터 흐름

```
FC 보드 (MAVLink UART)
  ↓
mavlink_bridge_app (C MAVLink 파싱)
  ↓ cFS SB publish
  - FC_ATTITUDE_STATE_MID   (MID 0x1900계열)
  - FC_EKF_LOCAL_STATE_MID
  - FC_GPS_RAW_STATE_MID
  - FC_EKF_STATUS_MID
  - SYSTEM_HEALTH_MID
  ↓
lora_tdm_app (SB 구독 → ASCII CSV 빌드 → LoRa UART 전송)
  ↓ LoRa 무선 전송 → COM7 (Windows)
fc_serial_ws_server.py (ASCII CSV 파싱 → JSON)
  ↓ WebSocket ws://127.0.0.1:8765
OpenMCT (Vite + JavaScript)
```

---

## 3. 확인된 사항 (체크리스트)

### ✅ 확인 완료
- [x] `lora_tdm_app` 빌드 성공 (4개 컴파일 에러 해결)
- [x] cFS 런타임에서 `LORA_TDM_APP` 정상 시작 확인
- [x] `mavlink_bridge_app` C 파싱 로그에서 ATTITUDE/GPS 디코딩 확인
  - `ATTITUDE decoded seq=NNN roll=X.XX pitch=Y.YY yaw=Z.ZZ`
  - `GPS_RAW_INT decoded seq=NNN fix=0 sats=0`
  - `LOCAL_POSITION_NED decoded` 확인
- [x] `lora_tdm_app_dispatch.c`: Unknown MID 0x1904 에러 → FC MID 라우팅 추가로 해결
- [x] 구조체 레이아웃 비교
  - `MAVLINK_BRIDGE_APP_AttitudeTlm_t` vs `AttMsg_t` (lora_tdm_app) → **일치**
  - `MAVLINK_BRIDGE_APP_EkfLocalTlm_t` vs `LocalMsg_t` → **일치**
  - `MAVLINK_BRIDGE_APP_GpsRawTlm_t` vs `GpsMsg_t` → 아래 상세 분석 필요
- [x] SH 패킷 (heartbeat, health_state)은 OpenMCT에서 정상 수신

### ❓ 미확인 / 의심
- [ ] `lora_tdm_app`이 실제로 FC 패킷을 LoRa로 전송하는지 (TX 로그 없음)
- [ ] `PacketType` 스위칭 동작 확인 (FC_STATE vs SYSTEM_HEALTH 교번 전송)
- [ ] `mavlink_bridge_app` C 파싱에서 byte offset이 올바른지 (핵심 의심 지점)
- [ ] GPS 구조체 레이아웃 미스매치 가능성

---

## 4. 구조체 레이아웃 상세 비교

### Attitude (확인 완료 ✅)

**mavlink_bridge_app 발행 구조체** (`default_mavlink_bridge_app_msgstruct.h`):
```c
typedef struct {
    CFE_MSG_TelemetryHeader_t TelemetryHeader;
    uint32 TimestampMs; uint32 Seq;
    uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 Reserved;
    float RollRad; float PitchRad; float YawRad;
    float RollspeedRps; float PitchspeedRps; float YawspeedRps;
} MAVLINK_BRIDGE_APP_AttitudeTlm_t;
```

**lora_tdm_app 수신 캐스트** (`lora_tdm_app_utils.c`):
```c
typedef struct {
    CFE_MSG_TelemetryHeader_t Hdr;
    uint32 TimestampMs; uint32 Seq;
    uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 Reserved;
    float RollRad; float PitchRad; float YawRad;
    float RollspeedRps; float PitchspeedRps; float YawspeedRps;
} AttMsg_t;
```
→ **완전 일치**

### EKF Local (확인 완료 ✅)

**발행**:
```c
CFE_MSG_TelemetryHeader_t TelemetryHeader;
uint32 TimestampMs; uint32 Seq;
uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 Reserved;
float X_m; float Y_m; float Z_m; float Vx_mps; float Vy_mps; float Vz_mps;
```

**수신 캐스트** (`LocalMsg_t`):
```c
CFE_MSG_TelemetryHeader_t Hdr;
uint32 TimestampMs; uint32 Seq;
uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 Reserved;
float X_m; float Y_m; float Z_m; float Vx_mps; float Vy_mps; float Vz_mps;
```
→ **완전 일치**

### GPS Raw (미확인 ❓)

**발행** (`MAVLINK_BRIDGE_APP_GpsRawTlm_t`):
```c
CFE_MSG_TelemetryHeader_t TelemetryHeader;
uint32 TimestampMs; uint32 Seq;
uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 FixType;
uint8 SatellitesVisible; uint8 Reserved; /* ← 6개 uint8 → 패딩 2바이트? */
int32 LatE7; int32 LonE7; int32 AltMm;
```
> ⚠️ uint8 × 6개 후 int32 → 컴파일러 2바이트 패딩 삽입 가능. Reserved가 1개면 3바이트 패딩.

**수신 캐스트** (`GpsMsg_t`):
```c
CFE_MSG_TelemetryHeader_t Hdr;
uint32 TimestampMs; uint32 Seq;
uint8 Valid; uint8 Stale; uint8 ErrorCode; uint8 FixType;
uint8 SatellitesVisible; uint8 Reserved; /* ← 동일 */
int32 LatE7; int32 LonE7; int32 AltMm;
```
→ 필드 나열은 동일하나 **실제 `Reserved` 개수 확인 필요**

---

## 5. PacketType 로직 (핵심)

`lora_tdm_app_utils.c`의 `UpdateCacheFromMsg`:
- `EKF_STATUS_MID` 수신 시 → `PacketType = FC_STATE_PACKET_TYPE (1)`
- `SYSTEM_HEALTH_MID` 수신 시 → `PacketType = SYSTEM_HEALTH_PACKET_TYPE (2)`
- ATTITUDE/LOCAL/GPS MID 수신 시 → PacketType **변경 없음**

`RunTx` 로직:
```c
if (PacketType == SYSTEM_HEALTH_PACKET_TYPE)  // ==2
    BuildShDownlinkLine(...)
else                                           // ==0 or 1 → FC 패킷
    BuildFcDownlinkLine(...)
```

**문제 시나리오**:
- 초기 `PacketType = 0` → FC 패킷 전송 (FcState 모두 0)
- `SYSTEM_HEALTH_MID` 수신 → PacketType = 2 → SH 패킷만 전송
- `EKF_STATUS_MID`가 오지 않으면 PacketType이 2에서 1로 돌아오지 않음
- 로그에서 EKF_STATUS_REPORT 수신 확인 안 됨 (GPS fix=0, EKF 미초기화)

> ⚠️ **가장 유력한 원인**: EKF_STATUS_REPORT 미수신 → PacketType이 SH(2)에 고정 → FC 패킷 미전송

---

## 6. mavlink_bridge_app C 파싱 의심 지점

이전에 Python `pymavlink`로 했을 때 동작, C로 전환 후 미동작.

확인할 파일: `mavlink_bridge_app/fsw/src/mavlink_bridge_app_utils.c`

의심 포인트:
1. `MAVLINK_BRIDGE_APP_ReadFloatLE()` 함수의 바이트 오프셋
2. MAVLink 메시지 헤더 크기 (v1: 6바이트, v2: 10바이트) 처리
3. 각 메시지 타입의 payload 필드 오프셋 (MAVLink 정의와 일치하는지)
4. `Valid` 플래그를 1로 설정하는 조건

---

## 7. 근본 원인 (확정) ✅

### 파이프 오버플로우 + PacketType 경쟁 조건

**설정값**:
- `LORA_TDM_APP_CYCLE_PERIOD_MS = 1000` (1초마다 RunCycle)
- SB 파이프 깊이 = **10**
- ATTITUDE 발행 주기 = ~50Hz (20ms마다)

**문제 발생 과정**:
1. RunCycle이 1초마다 파이프를 드레인
2. ATTITUDE 50Hz → 20ms마다 도착 → **200ms만에 파이프(깊이 10) 가득 참**
3. 이후 200ms ~ 1000ms 구간에 도착하는 모든 메시지 **드롭**
   - EKF_STATUS (5Hz, 200ms마다) → 드롭
   - SYSTEM_HEALTH (1Hz, 1000ms마다) → **간혹 파이프가 가득 차기 전에 도착하면 저장됨**
4. 파이프 드레인 후 처리되는 10개 메시지: 전부 ATTITUDE (PacketType 변경 없음)
5. 하지만 SYSTEM_HEALTH가 초기에 들어갔다면 → `PacketType = 2 (SH)` 고정
6. EKF_STATUS가 한 번도 파이프에 못 들어감 → PacketType 1(FC)로 복귀 불가
7. **결과: SH 패킷만 전송, FC 패킷 전혀 전송 안 됨**

> **경쟁 조건(Race Condition)**: 파이프에 어떤 메시지가 살아남느냐가 타이밍에 따라 매번 달라짐. 같은 코드인데 실행할 때마다 결과가 다른 것 = 경쟁 조건.

### lora_tdm_app 수정 내용 (적용 완료)

**수정 1: 파이프 깊이 10 → 50** (`lora_tdm_app.c`, Init)
```c
Status = CFE_SB_CreatePipe(&LORA_TDM_APP_Data.CommandPipe, 50, "LORA_TDM_PIPE");
```
> `CFE_PLATFORM_SB_MAX_PIPE_DEPTH = 50`은 이 프로젝트 설정값 (변경 가능).
> 단, 50도 ATTITUDE 50Hz + 1초 주기 기준 꽉 참 → 근본 해결은 아님.
> 200 시도 시 `CreatePipeErr: Bad Input Arg` 로 앱 즉시 종료됨 (확인됨).

**수정 2: PacketType 메시지 의존 방식 → DownlinkSeq 기반 결정적 교번** (`lora_tdm_app.c`, RunTx)
```c
// 짝수 seq → FC 패킷, 홀수 seq → SH 패킷 (deterministic TDM)
Type = ((LORA_TDM_APP_Data.DownlinkSeq % 2U) == 0U)
           ? LORA_TDM_APP_FC_STATE_PACKET_TYPE
           : LORA_TDM_APP_SYSTEM_HEALTH_PACKET_TYPE;
```
- EKF_STATUS 미수신 상태에서도 FC/SH 1:1 교번 보장
- 경쟁 조건 제거

---

## 8. 앱 리네임 및 현재 빌드 구조 (2026-05-25~)

`lora_tdm_app` → `lora_fc_downlink_app`으로 리네임됨.

| 항목 | 내용 |
|------|------|
| `targets.cmake` | `lora_fc_downlink_app` 참조 |
| `startup.scr` | `lora_fc_downlink_app` 로드 |
| 소스 위치 | `~/cFS_clean/apps/lora_fc_downlink_app/` |
| 이전 소스 | `~/cfs-telemetry-app/lora_tdm_app/` (빌드에서 제외) |

### lora_fc_downlink_app 아키텍처 차이

| | lora_tdm_app | lora_fc_downlink_app |
|--|--|--|
| 처리 방식 | Poll (1초 주기 RunCycle) | 이벤트 기반 (PEND_FOREVER) |
| LoRa 쓰기 | 1초마다 1회 | 메시지 수신마다 즉시 |
| 파이프 오버플로우 | 발생 | 거의 없음 (즉시 소비) |

---

## 9. lora_fc_downlink_app 추가 버그 및 수정 (2026-06-14) ✅

### 발견된 버그

Python 브리지(`fc_serial_ws_server.py`)가 기대하는 형식과 불일치:

| 패킷 | lora_fc_downlink_app 출력 | Python 브리지 필요 | 결과 |
|------|--------------------------|-------------------|------|
| FC | 15 fields | 17 fields (`uplink_fb` 누락) | `[BAD]` → 드롭 |
| SH | 5 fields | 7 fields (`link_state`, `uplink_fb` 누락) | `[BAD]` → 드롭 |
| TX 속도 | 50Hz (ATTITUDE마다 쓰기) | 제한 없음 | LoRa 버퍼 오버플로 |

### 수정 내용 (`lora_fc_downlink_app_utils.c`, `lora_fc_downlink_app.h`)

```c
// SH: ,0,0 추가 (link_state=0, uplink_fb=0)
"SH,%lu,%lu,%u,%u,0,0\n"

// FC: ,0 추가 (uplink_fb=0)
"FC,%lu,%lu,%.6f,...,%u,0\n"

// 레이트 리미터: 500ms 미만이면 쓰기 스킵
if ((NowMs - LORA_FC_DOWNLINK_APP_Data.LastLoRaTxMs) < 500U) return;
LORA_FC_DOWNLINK_APP_Data.LastLoRaTxMs = NowMs; // 쓰기 성공 시 갱신
```

`lora_fc_downlink_app.h`에 `uint32 LastLoRaTxMs` 필드 추가.

### 수정 후 예상 동작

ATTITUDE 50Hz 도착 → 500ms마다 1회만 FC 쓰기 (2Hz).
SYSTEM_HEALTH 1Hz 도착 → 500ms 경과 시 SH 쓰기.
결과: FC(0ms) → FC(500ms) → SH(1000ms) → FC(1500ms) ... 교번 패턴.

---

## 10. 패킷 포맷 불일치 (openmct_bridge_notes.md vs 실제 코드)

`openmct_bridge_notes.md`에 문서화된 포맷과 `fc_serial_ws_server.py`가 실제로 파싱하는 포맷이 다름.

| | 문서 (bridge_notes) | 파서 코드 (fc_serial_ws_server.py) |
|--|--|--|
| FC fields | 16개 (uplink_fb 없음) | 17개 필요 (`len(parts) >= 17`) |
| SH fields | 5개 (link_state, uplink_fb 없음) | 7개 필요 (`len(parts) >= 7`) |

**이유**: `lora_tdm_app`은 `uplink_fb`, `link_state` 필드를 전송했었음. `lora_fc_downlink_app`으로 리네임되면서 이 필드들이 누락됨. 문서는 업데이트됐지만 파서 코드는 구버전 포맷 기준으로 남아 있음.

**수정 방향**: `lora_fc_downlink_app`이 `,0` / `,0,0` 추가 (실제 값 없으므로 0 하드코딩) → 파서 코드 변경 없이 호환.

---

## 11. 설계 원칙 vs 현재 구현 불일치 (cFS설명.pdf 기준)

PDF(`cFS설명.pdf`)에 명시된 원래 설계 의도:

> **lora_tdm_app**: "LoRa 시리얼 **독점 소유**. 1Hz TDM: TX → RX 300ms 윈도우. 수신 프레임 CRC16 검증 후 **UPLINK_APP_CMD_MID로 SB 전달**"
>
> **uplink_app**: "명령 수신: **lora_tdm_app으로부터** 지상국이 보낸 명령을 받습니다"

즉 `uplink_app`은 시리얼 포트를 열지 않고 **SB 메시지만 수신**하는 것이 설계 의도.

```
설계 의도 (PDF):
지상국 → LoRa RF → Pi CP2102
                        ↓
              lora_tdm_app (포트 독점)
              TX 후 RX 300ms 윈도우
              CRC16 검증
                        ↓ SB publish (UPLINK_APP_CMD_MID)
              uplink_app (시리얼 포트 미접촉)
              시퀀스 검증 → 헬스 게이트 → cfs_core_app
```

---

## 12. LoRa 시리얼 포트 충돌 (소프트웨어 — 설계 원칙 위반)

`lora_tdm_app` → `lora_fc_downlink_app` 재설계 과정에서 핵심 원칙 3개가 사라짐:

| 원칙 | lora_tdm_app (PDF 설계) | lora_fc_downlink_app (현재 코드) |
|------|------------------------|--------------------------------|
| 시리얼 포트 독점 | ✅ lora_tdm_app만 소유 | ❌ uplink_app도 직접 열음 |
| uplink 전달 방식 | ✅ SB publish → uplink_app | ❌ uplink_app이 시리얼 직접 읽음 |
| TDM RX 윈도우 | ✅ TX 후 300ms 명시적 대기 | ❌ 없음 (이벤트 기반 TX만) |

**결과**: `uplink_app`과 `lora_fc_downlink_app`이 동일 포트(CP2102)를 별도 fd로 동시에 열고 있음.

```
uplink_app:            open(CP2102, O_RDONLY)
lora_fc_downlink_app:  open(CP2102, O_RDWR)
```

Linux에서 같은 시리얼 포트를 두 프로세스가 열면 **먼저 read()한 쪽이 바이트를 가져감** → 나머지는 영구 손실.

```
Pi CP2102 수신 버퍼: [U][P][,][1]...
                              ↓
              ┌───────────────┴───────────────┐
         uplink_app                  lora_fc_downlink_app
         (read 루프)                  (ServiceLoRaRead)
              │                               │
         UP 바이트 일부 가져감          HB 바이트 일부 가져감
         → UP 프레임 파싱 실패          → ACK 미수신
```

**올바른 수정 방향**:
```
lora_fc_downlink_app  ← 포트 독점 (TX + RX 300ms 윈도우)
                              │ UP 프레임 수신 시 CRC16 검증 후
                         SB publish (UPLINK_APP_CMD_MID)
                              │
                         uplink_app ← 시리얼 포트 닫고 SB 구독만
```

---

## 13. LoRa RF 충돌 (하드웨어 / 반이중 문제)

`openmct_bridge_notes.md`에서 이미 파악된 문제.

**원인**: LoRa는 반이중(half-duplex). Pi가 FC/SH 다운링크를 보내는 동안 Windows에서 UP 업링크를 보내면 **동일 RF 채널에서 충돌** → 프레임 깨짐.

```
EVS: UPLINK_APP: LoRa frame parse failed: UP1,1,10,...
```

PDF 설계의 TDM이 이 문제를 해결하는 방식:
```
Pi TX (FC/SH 전송)
       ↓
RX 윈도우 300ms 오픈  ← 이 구간에만 Windows가 전송 가능
       ↓
Pi RX (HB or UP 수신)
       ↓
다음 TX 사이클
```

Pi가 TX를 마친 직후 명시적으로 RX 윈도우를 열어야 하며, Windows는 그 윈도우 안에서만 전송해야 함. `lora_fc_downlink_app`은 이 윈도우 메커니즘이 없어서 충돌 방어 불가.

**근본 해결**: LoRa 모듈 2개로 분리 (TX 전용 / RX 전용). Windows 쪽 COM6 후보 논의 중.

---

## 14. 전체 미해결 이슈 목록 (2026-06-14 기준)

| # | 이슈 | 심각도 | 상태 |
|---|------|--------|------|
| 1 | `lora_fc_downlink_app` FC/SH 패킷 포맷 불일치 | 🔴 치명 | ✅ 수정 완료 (WSL2 빌드) |
| 2 | `lora_fc_downlink_app` 50Hz TX → LoRa 과부하 | 🔴 치명 | ✅ 500ms 레이트 리미터 추가 |
| 3 | `uplink_app`이 직접 시리얼 포트 열기 (설계 원칙 위반) | 🔴 치명 | ❌ 미해결 — `lora_fc_downlink_app`이 포트 독점 후 SB publish로 전달해야 함 |
| 4 | TDM RX 윈도우 없음 → RF 반이중 충돌 방어 불가 | 🟠 높음 | ❌ 미해결 — TX 후 300ms RX 윈도우 복원 필요 |
| 5 | LoRa RF 하드웨어 충돌 (Pi TX 중 Windows TX) | 🟠 높음 | ❌ 미해결 (LoRa 모듈 2개 분리 또는 TDM 복원) |
| 6 | `CFE_PLATFORM_SB_MAX_PIPE_DEPTH=50` 한계 (구버전) | 🟡 중간 | ✅ lora_fc_downlink_app은 PEND_FOREVER로 우회 |
| 7 | RSSI/SNR 미지원 (LoRa 투명 UART 모드) | 🟢 낮음 | 하드웨어 모드 변경 필요 |
| 8 | GPS `sats` 필드 미전송 | 🟢 낮음 | 패킷 포맷 확장 필요 |

---

## 15. 관련 파일 경로

| 파일 | 용도 |
|------|------|
| `~/cFS_clean/apps/lora_fc_downlink_app/fsw/src/lora_fc_downlink_app_utils.c` | LoRa TX/RX + 패킷 빌드 (수정됨) |
| `~/cFS_clean/apps/lora_fc_downlink_app/fsw/src/lora_fc_downlink_app.h` | Data 구조체 (LastLoRaTxMs 추가됨) |
| `~/cFS_clean/apps/uplink_app/fsw/src/uplink_app_utils.c` | LoRa RX 업링크 파싱 (포트 충돌 대상) |
| `~/cfs-telemetry-app/mavlink_bridge_app/fsw/src/mavlink_bridge_app_utils.c` | C MAVLink 파싱 로직 |
| `~/cfs-telemetry-app/lora_tdm_app/fsw/src/lora_tdm_app.c` | 구버전 TDM 앱 (수정 적용됨, 현재 빌드 제외) |
| `/mnt/c/.../openMCT/fc_serial_ws_server.py` | 지상국 ASCII 파서 + WS 브로드캐스트 |
| `/mnt/c/.../openMCT/openmct_bridge_notes.md` | 전체 데이터 흐름 참고 문서 |

