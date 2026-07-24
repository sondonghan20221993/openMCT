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
- [x] 커밋 + push (`1b2d2f2`)

## 2차 범위 — UplinkHandler HTTP 통합테스트 (2026-07-14 착수)

1차에서 "필요시 2차로" 미뤄뒀던 `UplinkHandler`(`BaseHTTPRequestHandler`) 자체를
지금 진행한다.

### 이유

순수 함수(CRC/페이로드/플래그/토큰)는 1차에서 커버됐지만, HTTP 요청→JSON
파싱→검증→라우팅→응답까지 이어지는 엔드투엔드 경로는 여전히 미검증:
- 잘못된 JSON body → 400 응답
- 필수 필드 누락(`scope`/`param`/`route_type`/`waypoints`) → 400 + 에러 메시지
- 정상 요청 → 200 + `queued=true` + `_pending_uplink`에 실제로 적재되는지
- `force` 플래그, `waypoints` 개수 제한(`MAX_ROUTE_WAYPOINTS`), CORS/OPTIONS

### 결정

`ThreadingHTTPServer`를 임의 포트(`0`)로 실제 기동해 `http.client`로 실제 HTTP
요청을 보내는 통합테스트로 작성 (`unittest`, 기존 `tests/` 디렉터리에 파일 추가:
`tests/test_uplink_handler_integration.py`). 서버는 각 테스트 클래스
`setUpClass`/`tearDownClass`에서 기동/종료. `_seq_counter`/`_pending_uplink`
등 모듈 전역 상태는 `setUp`에서 초기화해 테스트 간 격리.

### 범위

- CONFIG: 정상 200, 알 수 없는 scope/param 400, value 비정수 400, uint32
  범위초과 400, force 플래그 반영
- ROUTE: 정상 200, 알 수 없는 route_type 400, waypoints 개수 초과/미만 400,
  waypoint 형식 오류 400
- RECOVERY: 정상 200 + `request_token` 응답 필드 존재, invalid payload_hex 400
- 공통: `/health` 200, `/api/uplink/meta` 200 스코프 목록, 알 수 없는 경로 404,
  잘못된 JSON body 400, OPTIONS 프리플라이트 204 + CORS 헤더

### 상태

- [x] 범위 결정 (본 섹션)
- [x] `tests/test_uplink_handler_integration.py` 작성 — CONFIG/ROUTE/RECOVERY
      정상·오류 케이스 + 공통(health/meta/404/잘못된 JSON/OPTIONS) 20개 케이스
- [x] 로컬 실행 검증 — 단독 20/20 PASS, 전체 스위트(1차 26 + 2차 20) 46/46 PASS,
      상호 간섭 없음 확인
- [x] 커밋 + push

## 3차 범위 — COUNTER/FLIGHT_MODE/RouteReadback 추가 (2026-07-22~24, 이 문서 갱신 누락분 정리)

이 문서가 2차(46개) 이후 갱신되지 않아 실제 테스트 스위트와 어긋나 있던 것을
2026-07-24에 정리. 그동안 `tests/test_uplink_handler_integration.py`에 아래가
추가·커밋됨(전부 동일한 `ThreadingHTTPServer` 실통합테스트 패턴):

- **COUNTER**(class 7, §18.4.6.7, 2026-07-22): `CounterEndpointTest` — 4개 scope
  전송 성공, 알 수 없는 scope 400
- **FLIGHT_MODE**(class 8, BL-44 §18.4.6.8, 2026-07-24): `FlightModeEndpointTest` —
  HOVER/WAYPOINT(+waypoint_start_index)/LAND 정상, 알 수 없는 mode 400,
  HOVER·LAND에 waypoint_start_index≠0 거부, uint8 범위초과 400 (7개)
- **RouteReadbackStatus**(0x1913 readback GUI 노출, 2026-07-24): `RouteReadbackStatusEndpointTest` —
  idle/pending/complete 상태 전이, waypoint 재조립 결과 확인 (3개)

### 상태

- [x] COUNTER 테스트 추가·커밋(2026-07-22)
- [x] FLIGHT_MODE 테스트 추가·커밋(2026-07-24, 커밋 `2d455a0`)
- [x] RouteReadbackStatus 테스트 추가·커밋(2026-07-24, 커밋 `2d455a0`)
- [x] 로컬 실행 검증 — 전체 스위트 38/38 PASS
- [x] 이 계획 문서 갱신(누락분 정리)
