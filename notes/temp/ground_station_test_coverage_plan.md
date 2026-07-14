# 지상국(fc_serial_ws_server.py) 테스트 커버리지 도입 (2026-07-14 도출)

## 문제

`fc_serial_ws_server.py`에 프로토콜 인코딩/인증/파싱 로직이 계속 늘고 있는데
(CONFIG/ROUTE/RECOVERY, §18.11.1 인증플래그, RECOVERY request_token 자동생성 등)
테스트가 전혀 없다. 방금 고친 RECOVERY request_token 누락도 이런 조용한 인코딩
버그의 실례 — 발견까지 우연에 의존했다.

## 왜 문제인가

- CRC/페이로드 바이트 레이아웃/인증플래그 계산은 기체측(`uplink_app_cmds.c`,
  `uplink_app_utils.c`)과 바이트 단위로 맞아야 하는데, 어긋나도 서버는 예외 없이
  "queued"만 응답한다 — 실기체에서만(그것도 무응답/거부로만) 드러남.
- cfs-telemetry-app 쪽(`tests/test_lora_downlink_decoder.py`,
  `tests/test_mission_upload_diag.py`)은 이미 이런 지상 스크립트 테스트 관행이
  있는데, openMCT(지상국) 레포에는 대응 관행이 없어 비대칭.

## 결정

이 레포에 `tests/` 디렉터리를 신설, 표준 라이브러리 `unittest`로 작성
(레포에 pytest 미설치 — 새 의존성 추가 없이 `python3 -m unittest discover`로
바로 실행 가능하게 함).

### 범위 (1차 — 순수 함수 위주)

- `_crc16` — 알려진 입력/출력 벡터, 빈 입력
- `_build_lora_frame` — 필드 순서/CRC 접미사 형식 검증
- `_auth_level_flag_bits` — CONFIG=2, ROUTE_UPDATE=2, RECOVERY=3 → 비트[7:6] 매핑,
  미등록 클래스 → 0
- `_generate_request_token` — 반복 호출 시 항상 0이 아님(0 fallback→1 분기 포함),
  32비트 범위 내
- `_config_checksum` / `_build_config_payload` — 헤더 필드 순서, uint32 LE 인코딩
- `_build_route_payload` — waypoint 개수/좌표 f32 LE 인코딩
- `_SeqCounter` — 1부터 시작, 0xFFFF 다음 wraparound(1로) 확인
- `parse_int` / `parse_float` — 정상/비정상 입력
- `parse_lora_line` — FC/SH 정상 라인, 필드 부족(반환 None), `sats`(idx17) 존재/부재,
  `rollspeed/pitch/yawspeed`(idx19~21, len>=22 조건) 존재/부재 경계값
- RECOVERY 페이로드 조립 — 현재 `_handle_recovery` 내부에 인라인돼 있어 순수
  함수로 분리 필요(`_assemble_recovery_payload(payload_hex) -> bytes`, 동작
  변경 없음): 4바이트 미만 패딩, 뒤 4바이트가 항상 토큰으로 덮어써지는지 검증

### 범위 밖(1차 제외)

- `UplinkHandler`(HTTP 핸들러) 자체 — `BaseHTTPRequestHandler` 통합테스트는
  비용 대비 낮은 우선순위, 필요시 2차로
  (`_handle_config`/`_handle_route`/`_handle_recovery`의 로직은 위 순수 함수
  분리로 대부분 커버됨)
- 시리얼/WebSocket 비동기 루프(`serial_reader`, `main_async`) — 하드웨어/IO 결합,
  이번 범위 아님

## 상태

- [x] 범위 계획 수립
- [x] `_handle_recovery`에서 RECOVERY 페이로드 조립 로직을 순수 함수로 추출
      (`_assemble_recovery_payload`, 동작 변경 없음)
- [x] `tests/test_fc_serial_ws_server.py` 작성 — crc16/build_lora_frame/
      auth_level_flag_bits/generate_request_token/config·route payload/
      assemble_recovery_payload/SeqCounter/parse_int·float/parse_lora_line
      (FC/SH 정상·부족필드·sats·rollspeed 경계값) 26개 케이스
- [x] 로컬 실행 검증(`python3 -m unittest discover -s tests`) — 26/26 PASS
- [ ] 커밋 + push
