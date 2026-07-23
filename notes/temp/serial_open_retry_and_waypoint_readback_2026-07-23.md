# 지상국 변경 (2026-07-23)

## 1. 시리얼 포트 open() 재시도 누락 수정

**증상**: `python fc_serial_ws_server.py` 실행 시 COM 포트가 다른 프로세스에
점유돼 있으면(`PermissionError`) 즉시 크래시하고 종료됨.

**원인**: `autodetect_serial_port()`엔 "모듈 미감지" 시 재시도 로직이
있었으나(`with_retry`), 포트를 찾은 **뒤** 실제로 여는 `serial.Serial(...)`
호출 자체는 재시도 없이 예외를 그대로 던짐 — 두 단계가 분리돼 있어서
발견이 늦었음(자동탐지는 성공했는데 open()에서 죽는 케이스).

**수정**: `_open_serial_with_retry()` 신설(`fc_serial_ws_server.py`) —
`serial.SerialException` 발생 시 `--no-lora-retry` 옵션과 연동해
`retry_interval`(5초)마다 계속 재시도, autodetect와 동일한 로그 스타일.
`main_async()`의 `serial.Serial()` 직접 호출을 이걸로 교체.

## 2. waypoint readback 지상 디코더 지원 (BL 관련, cfs-telemetry-app 측
   `lora_protocol_v2_spec.md` §4.3 참조)

기체(`lora_tdm_app`)가 DL2 확장 블록(`flags` bit2)으로 mission route를
페이지 단위(waypoint 2개씩) 다운링크하는 기능을 오늘 신설(cfs-telemetry-app
커밋 `3989d5c`) — 지상 쪽도 대응:

- `lora_protocol_v2.py`: `Dl2Frame`에 `wp_route_type`/`wp_page_index`/
  `wp_total_pages`/`wp_waypoints` 필드 추가, `decode_dl2`/`encode_dl2`에
  파싱/인코딩 반영, `DownlinkStream` 프레임 길이 상한 확장.
- `RouteReadbackAssembler` 신설 — 페이지를 순서대로 `feed()`하면
  `total_pages`만큼 모였을 때 전체 waypoint 리스트 반환. 페이지 누락은
  자동 재시도 없음(지상이 DIAGNOSTIC 요청 재전송으로 처리, 단순화 결정).
- `tests/test_lora_protocol_v2_waypoint.py` 신규 8케이스, 전부 PASS.

**미완**: GUI 패널(현재는 로직만, 화면 표시 없음), `fc_serial_ws_server.py`에
실제 배선(RouteReadbackAssembler 인스턴스화 + WS 브로드캐스트)은 아직
안 함 — Pi 실기 검증 때 필요하면 이어서.
