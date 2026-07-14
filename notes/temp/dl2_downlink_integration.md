# fc_serial_ws_server.py에 DL2(v2) 다운링크 파싱 통합 (2026-07-14 도출)

## 문제

Pi(`lora_tdm_app`)를 CONFIG 명령으로 v2(DL2 바이너리) 다운링크로 전환했더니,
실제로 돌고 있는 지상국(`fc_serial_ws_server.py`)이 이를 못 읽고 깨진 바이트를
그대로 출력했다. 원인: `fc_serial_ws_server.py::serial_reader()`가
`readline()` + `parse_lora_line()`(v1 ASCII 전용)만 쓰고, DL2 바이너리 파싱
경로가 아예 없음.

## 발견 — 이미 완성된 v2 파서가 방치돼 있었음

`lora_protocol_v2.py`(이 레포, 미커밋 상태)에 DL2 디코더/ACK2 빌더/UP2
인코더/`DownlinkStream`(v1+v2 자동판별 바이트스트림 파서)이 **이미 완성돼
있으나 `fc_serial_ws_server.py`에 연결된 적이 없다** — 별도 CLI 스크립트로만
존재. 게다가 **44바이트 구버전**이라 `cfs-telemetry-app/bridge/
lora_downlink_decoder.py`(같은 로직의 자매 파일, 이번 세션에서 SYSTIME 크래시
수정 + sats 필드 45바이트 반영까지 끝난 최신본)와도 어긋나 있었다.

## 결정

1. `lora_protocol_v2.py`를 `cfs-telemetry-app/bridge/lora_downlink_decoder.py`
   최신본 기준으로 전체 동기화(45바이트+sats+SYSTIME 길이가드+UP2 인코더 포함).
2. `fc_serial_ws_server.py::serial_reader()`를 `readline()` 기반에서
   `DownlinkStream.feed()` 기반으로 교체 — v1/v2 프레임을 매직바이트로
   자동 판별해 혼용 처리(spec §8 공존 규칙과 일치).
3. `Dl2Frame` → 기존 WS 브로드캐스트/CSV 스키마(`_csv_fields`)로 매핑하는
   `dl2_frame_to_data()` 헬퍼 추가 — 기존 필드명(roll/pitch/yaw/x/y/z/...,
   health_state/fault_code/link_state)에 맞춰 변환, 스키마 변경 없음.
   (`sys_time_unix_usec`/`pos_saturated`는 이번 범위에서 CSV에 반영 안 함 —
   필요해지면 `_csv_fields` 확장 별도 진행)
4. DL2 프레임 수신 시 응답은 기존 `_send_ack()`(v1 "ACK,seq" 텍스트) 대신
   `build_ack2()`(바이너리 ACK2)로 분기.
5. 업링크(CONFIG/ROUTE/RECOVERY) 프레임은 이번 범위에서 변경 없음 — v1 "UP,..."
   텍스트 그대로 유지(spec상 업/다운 프로토콜은 방향별 독립, 다운링크만 v2로
   전환해도 업링크는 v1으로 공존 가능).

## 상태

- [x] 문제 확인 + 원인 파악 (2026-07-14)
- [x] 계획 수립 (본 문서)
- [x] `lora_protocol_v2.py` 최신본 동기화 — `cfs-telemetry-app/bridge/
      lora_downlink_decoder.py` 전체 복사(45바이트+sats+SYSTIME 길이가드+
      UP2 인코더 포함, 두 레포 소스 동일하게 맞춤)
- [x] `fc_serial_ws_server.py` `serial_reader()` DownlinkStream 통합 —
      `_ser.readline()` → `_ser.read(256)` + `DownlinkStream.feed()`로 교체
      (readline은 DL2 바이너리 안에 우연히 0x0A가 섞이면 프레임을 끊어버릴
      위험이 있어 애초에 부적합했음)
- [x] `dl2_frame_to_data()` 매핑 헬퍼 + `_lora_send_ack2()`(바이너리 ACK2,
      개행 없음) 구현. `_lora_send_bytes()`로 텍스트/바이너리 전송 경로 통합
- [x] 기존 테스트(46건) 회귀 없음 + DL2 통합 신규 테스트 4건 추가(필드매핑/
      스케일링, link quality seq 갱신, CSV 스키마 정합성, DL2_BASE_LEN 동기화
      회귀가드) — 전체 50/50 PASS
- [x] 커밋 + push
- [ ] Pi를 v1으로 되돌린 뒤(사용자 작업), 지상국 재시작 → v2 재전환 → 실제 파싱 확인
