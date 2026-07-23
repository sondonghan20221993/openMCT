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

**부가**: 실제로 COM7이 점유돼 있던 원인이 죽지 않고 남아있던 이전
`fc_serial_ws_server.py` 인스턴스(PID)였음을 확인 — `--kill-stale`
CLI 플래그 신설(`kill_stale_server_processes()`). PowerShell
`Get-CimInstance Win32_Process`로 자기 자신의 PID(`os.getpid()`)를
제외한 동일 스크립트 프로세스를 찾아 `taskkill /F`. **주의(실측으로
발견)**: 처음엔 `CommandLine -match script_name`만으로 걸렀는데, 이
필터 자체를 실행하는 PowerShell 하위 프로세스의 CommandLine에도 검색
패턴 문자열이 그대로 들어있어 **자기 자신을 오탐**하는 버그가 있었음 —
`Name LIKE 'python%'`로 먼저 걸러 수정. Windows 전용(다른 OS는
아무 것도 안 하고 반환). 외부 의존성(psutil 등) 추가 없이 PowerShell만
사용. 실측: PID 23684(구 인스턴스) 정상 종료 확인.

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

## 3. DIAGNOSTIC 클래스(6) 지상 송신 경로 신설 (실기 검증 중 발견)

Pi 실기 검증(BL-40 RESTART 3종 PASS 확인 후) 도중 waypoint readback 요청을
보내려다 발견: **DIAGNOSTIC 클래스(class=6) 자체가 지상에 한 번도 구현된
적이 없었음** — 기존 LINK_STATUS/RX_STATS/TX_STATS(lora_tdm_app)도, 오늘
추가한 ROUTE_READBACK_REQUEST(cfs_core_app)도 보낼 방법이 없었음. spec
§4.3/본 문서 "미완" 항목에는 "GUI 패널 없음"으로만 기록돼 있었는데, 실제로는
그보다 근본적으로 **송신 API 자체가 없는 상태**였음(정정).

**수정**: `fc_serial_ws_server.py`에 `UPLINK_CLASS_DIAGNOSTIC=6` 신설,
`_handle_counter`와 동일 payload 구조(action(1)+target(1)+token(4) LE —
`uplink_app_utils.c ForwardDiagnosticCommand` 파싱 순서와 일치 확인)로
`_handle_diagnostic()` 핸들러 + `/api/uplink/diagnostic` 엔드포인트 추가.
`DIAG_TARGET_LORA_TDM=0`/`DIAG_TARGET_CFS_CORE=1`, `DIAG_ACTIONS` 매핑
(`route_readback`→3 등). `UPLINK_CLASS_REQUIRED_LEVEL[DIAGNOSTIC]=1`
(C측 `GetClassRequiredLevel` case DIAGNOSTIC→1과 일치 확인, level 3 아니라
request_token 필수 아님).

**미완**: GUI 버튼(uplinkGUI/plugin.js)·CLI 명령(uplinkCLI/plugin.js)은
아직 없음 — 지금은 HTTP API(`/api/uplink/diagnostic`)로만 호출 가능.
`RouteReadbackAssembler` 인스턴스화 + WS 브로드캐스트(수신 페이지 재조립
결과를 지상 화면에 표시)도 아직 안 됨.
