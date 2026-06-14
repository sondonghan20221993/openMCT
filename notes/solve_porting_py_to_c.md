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

### 수정 내용

**수정 1: 파이프 깊이 10 → 200** (`lora_tdm_app.c`, Init)
```c
// Before:
Status = CFE_SB_CreatePipe(&LORA_TDM_APP_Data.CommandPipe, 10, "LORA_TDM_PIPE");
// After:
Status = CFE_SB_CreatePipe(&LORA_TDM_APP_Data.CommandPipe, 200, "LORA_TDM_PIPE");
```

**수정 2: PacketType 메시지 의존 방식 → DownlinkSeq 기반 결정적 교번** (`lora_tdm_app.c`, RunTx)
```c
// 짝수 seq → FC 패킷, 홀수 seq → SH 패킷 (deterministic TDM)
Type = ((LORA_TDM_APP_Data.DownlinkSeq % 2U) == 0U)
           ? LORA_TDM_APP_FC_STATE_PACKET_TYPE
           : LORA_TDM_APP_SYSTEM_HEALTH_PACKET_TYPE;
```
- EKF_STATUS 미수신 상태에서도 FC/SH 1:1 교번 보장
- SYSTEM_HEALTH 타이밍 경쟁 조건 제거

---

## 8. 다음 조사 단계 (우선순위 순)

1. **PacketType 고착 확인**: cFS 로그에서 FC 패킷 전송 여부 확인
   - `lora_tdm_app`에 TX 로그 추가 또는 COM7에서 직접 모니터링
   
2. **EKF_STATUS_REPORT 수신 확인**: `mavlink_bridge_app` 로그에서 EKF 메시지 수신 여부 확인

3. **PacketType 로직 개선**: EKF_STATUS에만 FC 전환 의존 제거
   - ATTITUDE/LOCAL 수신 시에도 `PacketType = FC_STATE`로 설정하거나
   - TDM 카운터 기반 교번 방식으로 변경

4. **`mavlink_bridge_app_utils.c` C 파싱 검증**: 바이트 오프셋 확인

---

## 8. 관련 파일 경로

| 파일 | 용도 |
|------|------|
| `~/cfs-telemetry-app/mavlink_bridge_app/fsw/src/mavlink_bridge_app_utils.c` | C MAVLink 파싱 로직 |
| `~/cfs-telemetry-app/mavlink_bridge_app/config/default_mavlink_bridge_app_msgstruct.h` | 발행 구조체 정의 |
| `~/cfs-telemetry-app/lora_tdm_app/fsw/src/lora_tdm_app_utils.c` | SB 수신 캐스트 + FC 패킷 빌드 |
| `~/cfs-telemetry-app/lora_tdm_app/fsw/src/lora_tdm_app_dispatch.c` | MID 라우팅 |
| `/mnt/c/.../openMCT/fc_serial_ws_server.py` | ASCII CSV 파싱 → WebSocket |

