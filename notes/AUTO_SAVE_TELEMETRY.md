# 텔레메트리 자동저장 기능

**구현 일자**: 2026-07-09  
**상태**: ✅ 완료 (IndexedDB + CSV)

---

## 개요

openMCT에서 수신한 모든 텔레메트리 데이터를 **자동으로 2가지 방식**으로 저장합니다.

```
WebSocket 수신 (ws://127.0.0.1:8765)
  ↓
openMCT 플러그인
  ├─ UI 표시
  ├─ IndexedDB 저장 (브라우저 로컬)
  └─ WebSocket broadcast
                    ↓
         fc_serial_ws_server.py
           ├─ JSON 파싱
           └─ CSV 파일 저장 (서버 로컬)
```

---

## 1️⃣ IndexedDB (브라우저 로컬 저장)

### 특징

| 항목 | 설명 |
|------|------|
| **저장 위치** | 브라우저 메모리 (Chrome: ~/.config/google-chrome/Default/IndexedDB) |
| **용량** | ~50MB (브라우저 설정에 따라 다름) |
| **보존 기간** | 브라우저 cache 삭제 시까지 |
| **접근성** | JavaScript 코드에서 직접 쿼리 가능 |
| **개인정보** | 브라우저 로컬에만 저장 (외부 전송 없음) |

### 저장 방식

```javascript
// 1. 자동 초기화 (openMCT 시작 시)
initIndexedDB()
  → DB명: 'cfsRealtime'
  → Store명: 'telemetry'
  → Key: timestamp (타임스탬프 기준 정렬)

// 2. 메시지 수신 시마다 저장
socket.onmessage = (event) => {
    const timestamp = msg.timestamp || Date.now();
    const telemetryData = {roll, pitch, yaw, x, y, z, ...};
    saveTelemetryToIndexedDB(timestamp, telemetryData);  ← 자동
}

// 3. 데이터 구조
{
  timestamp: 1720550422000,
  data: {
    roll: 0.1234,
    pitch: -0.0567,
    yaw: 1.5678,
    x: 100.5,
    y: 200.3,
    z: -50.2,
    ...
  }
}
```

### 개발자도구에서 확인

```
Chrome 개발자도구 → Application → IndexedDB → cfsRealtime → telemetry
```

모든 저장된 항목이 시간순으로 나열됩니다.

### 조회 (JavaScript)

```javascript
// IndexedDB에서 모든 데이터 조회
getAllTelemetryFromIndexedDB()
  .then(records => {
    console.log('Total records:', records.length);
    records.forEach(r => {
      console.log(r.timestamp, r.data);
    });
  });

// 오래된 데이터 삭제 (예: 1시간 이전)
const oneHourAgo = Date.now() - 3600000;
clearOldTelemetry(oneHourAgo);
```

---

## 2️⃣ CSV 파일 (서버 자동 저장)

### 특징

| 항목 | 설명 |
|------|------|
| **저장 위치** | `telemetry_logs/telemetry_YYYYMMDD_HHMMSS.csv` |
| **용량** | 무제한 (디스크 용량에만 의존) |
| **보존 기간** | 파일 삭제 시까지 (영구 보존) |
| **접근성** | Excel, Python pandas, 텍스트 에디터로 열기 |
| **개인정보** | 로컬 파일 (외부 전송 없음) |

### 저장 방식

```python
# 1. 서버 시작 시 CSV 파일 생성
fc_serial_ws_server.py --baud 57600 --http-port 8082
  ↓
_init_csv()
  → telemetry_logs/telemetry_20260709_143022.csv 생성
  → CSV 헤더 작성

# 2. 각 downlink 수신 시마다 행 추가
serial_reader()
  ↓ downlink 라인 수신
  ↓ JSON 파싱
  ↓ _save_telemetry_to_csv(data)  ← 자동 추가
     (거의 실시간, ~50-100ms 주기)

# 3. 서버 종료 시 파일 정상 닫음
finally:
    if _csv_file:
        _csv_file.close()
```

### CSV 파일 구조

```csv
timestamp,source,roll,pitch,yaw,x,y,z,vx,vy,vz,lat,lon,alt,fix,seq,boot_ms,health_state,fault_code,heartbeat,packet_loss
2026-07-09T14:30:22.123456,FC,0.1234,-0.0567,1.5678,100.5,200.3,-50.2,5.1,3.2,-2.1,37.123456,127.654321,500,1,123,5000,0,0,10,0.5
2026-07-09T14:30:22.623456,SH,,,,,,,,,,,,,,,123,5000,0,0,11,0.5
2026-07-09T14:30:23.123456,FC,0.1245,-0.0568,1.5690,100.6,200.4,-50.3,5.2,3.3,-2.2,37.123500,127.654400,501,1,124,5100,0,0,12,0.4
...
```

### 필드 설명

| 필드 | 설명 | 출처 |
|------|------|------|
| `timestamp` | ISO 8601 형식 시간 | 서버 시각 |
| `source` | FC 또는 SH | downlink 패킷 타입 |
| `roll`, `pitch`, `yaw` | 자세 (rad) | FC |
| `x`, `y`, `z` | 위치 (m, NED) | FC |
| `vx`, `vy`, `vz` | 속도 (m/s, NED) | FC |
| `lat`, `lon` | 좌표 (deg) | FC (×1e-7 변환됨) |
| `alt` | 고도 (m) | FC (mm→m 변환됨) |
| `fix` | GPS fix type | FC |
| `seq` | 시퀀스 | FC/SH |
| `boot_ms` | 부팅 후 경과시간 | FC |
| `health_state` | 시스템 헬스 | SH |
| `fault_code` | 장애 코드 | SH |
| `heartbeat` | 수신 패킷 누적 수 | SH |
| `packet_loss` | 손실률 (%) | SH |

> **주의**: FC 패킷에서 SH 필드는 공란, SH 패킷에서 FC 필드는 공란.

---

## 사용 방법

### 1. 자동저장 시작

**Windows PowerShell**:
```powershell
python fc_serial_ws_server.py --baud 57600 --http-port 8082
```

**콘솔 출력**:
```
[SERIAL] opening COM7 @ 57600
[CSV] Created telemetry_logs/telemetry_20260709_143022.csv
[WS]    ws://127.0.0.1:8765  (telemetry)
[OK] FC,seq=123,... → CSV 저장
[OK] SH,seq=124,... → CSV 저장
...
```

### 2. openMCT UI 접속

```
http://localhost:5173
```

텔레메트리가 실시간으로 표시됩니다.

### 3. CSV 파일 확인

**Windows 탐색기**:
```
openMCT/ → telemetry_logs/ → telemetry_20260709_143022.csv
```

**Excel에서 열기**:
- UTF-8 인코딩 자동 감지
- 실시간 업데이트 (서버 실행 중)

**Python으로 분석**:
```python
import pandas as pd

df = pd.read_csv('telemetry_logs/telemetry_20260709_143022.csv')
print(df.describe())
print(df[['timestamp', 'roll', 'pitch', 'yaw']])

# FC 패킷만 필터
fc_df = df[df['source'] == 'FC']
print(fc_df.shape)
```

### 4. IndexedDB 확인

**Chrome DevTools**:
```
F12 → Application → IndexedDB → cfsRealtime → telemetry
```

브라우저 재시작 후에도 데이터 보존됨.

---

## 저장 주기

| 저장 방식 | 주기 | 버퍼링 |
|-----------|------|--------|
| **IndexedDB** | 메시지마다 (50~100ms) | 없음 (즉시 저장) |
| **CSV** | 메시지마다 (50~100ms) | flush 적용 (즉시 디스크 쓰기) |

→ **거의 실시간** 저장 (지연 무시할 수 있는 수준)

---

## 저장 용량

### IndexedDB
```
약 50MB × (메시지당 200바이트)
= ~250,000개 메시지 = ~42시간 (50Hz 기준)
```

→ 장시간 운영 시 `clearOldTelemetry()`로 정리 필요

### CSV
```
파일 크기 = 헤더(~200바이트) + 행(~150바이트) × 메시지 수
예) 50Hz × 3600초 = 180,000 메시지/시간
   = 180,000 × 150 = 27MB/시간
```

→ 디스크 용량만 충분하면 무제한

---

## 트러블슈팅

### Q: CSV 파일이 생성 안 됨

**A**: 
```bash
# 1. 디렉토리 확인
ls -la openMCT/telemetry_logs/

# 2. 권한 확인
chmod 755 openMCT/

# 3. 콘솔 로그 확인
[CSV] Created ... 메시지 있는지 확인
```

### Q: CSV 파일이 열리지 않음

**A**:
```
- 서버가 아직 실행 중인지 확인 (파일이 lock될 수 있음)
- 파일이 0 바이트인지 확인
- 인코딩: UTF-8 자동 감지
```

### Q: IndexedDB 용량 초과

**A**:
```javascript
// 오래된 데이터 삭제
const oneDayAgo = Date.now() - 86400000;
clearOldTelemetry(oneDayAgo);
```

---

## 향후 개선 (선택)

- [ ] CSV 열 선택 옵션 (모든 필드 vs 필요 필드만)
- [ ] 저장 주기 설정 (1초, 10초, 1분 등)
- [ ] IndexedDB → CSV 내보내기 버튼
- [ ] 저장 용량 모니터링 (대시보드)
- [ ] 자동 정리 정책 (오래된 데이터 자동 삭제)
- [ ] 압축 저장 (gzip)

---

## 관련 파일

| 파일 | 역할 |
|------|------|
| `my_openmct_app/src/plugins/cfsRealtime/plugin.js` | IndexedDB 저장 |
| `fc_serial_ws_server.py` | CSV 파일 생성/저장 |
| `telemetry_logs/` | CSV 파일 저장 디렉토리 |

---

**결론**: 
- ✅ **브라우저 로컬**: IndexedDB로 자동 저장
- ✅ **서버 로컬**: CSV로 자동 저장
- ✅ **실시간**: 50~100ms 주기로 저장
- ✅ **영구 보존**: CSV는 디스크에 영구 저장
- ✅ **0 클릭**: 실행하면 자동으로 됨
