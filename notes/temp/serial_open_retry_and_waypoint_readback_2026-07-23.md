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

**미완(갱신, 2026-07-23 저녁)**: `RouteReadbackAssembler`는 이제 콘솔
출력까지는 배선됨(`dl2_frame_to_data()`에서 페이지 수신마다 `[WP]` 로그,
완료 시 재조립 결과 출력 — 커밋 `7fb095a`). **WS 브로드캐스트(지상
화면 표시)와 GUI 버튼/CLI 명령은 여전히 없음** — HTTP API로만 트리거
가능.

## 4. COM 포트 점유 실제 원인 — QGroundControl 자동연결 (실측 확인)

`--kill-stale`로도 안 풀리던 `PermissionError`의 진짜 원인은 죽지 않은
서버 인스턴스가 아니라 **QGroundControl이 실행 중이면 감지되는 시리얼
포트(LoRa CP210x 포함)에 자동으로 연결을 시도**하는 것이었음
(`Get-Process`로 QGC 프로세스 확인 후 종료 → 즉시 해결). **운영 수칙**:
`fc_serial_ws_server.py`와 QGroundControl은 **동시에 같은 COM 포트를
못 씀** — 둘 중 하나만 그 포트를 물 수 있으므로, 지상 서버 실행 전
QGC가 떠 있으면 먼저 종료(또는 QGC의 자동연결 대상에서 그 포트 제외)
해야 함.

## 5. waypoint 유효성 제약 (실측 REJECT_ROUTE(UFB=9) 계기로 확인)

`uplink_app_utils.c UPLINK_APP_ParseRouteUpdatePayload()`가 강제하는
조건 — 임의 좌표로 테스트하다 거부당하기 쉬우므로 지상 쪽에도 기록:

- X/Y: `UPLINK_APP_ROUTE_FLYABLE_X/Y_MIN/MAX_M` = **±50m**
- 고도(Z): `UPLINK_APP_ROUTE_ALTITUDE_MIN/MAX_M` = **2.0m ~ 8.0m**
  (0 이하로 보내면 REJECT_ROUTE)
- **인접 waypoint 간 거리는 정확히 2.0m(±0.0001m)여야 함**
  (`UPLINK_APP_ROUTE_SEGMENT_DIST_M`/`_TOL_M`) — 임의의 거리 불가,
  사실상 딱 2.0m 간격으로만 설계 가능. waypoint 1개짜리는 이 검사
  자체가 적용 안 됨(세그먼트가 없으므로).
- 검증 통과 예시(2026-07-23 실사용, 실기 업로드 성공+FC 미션 반영
  확인): `(0,0,4)/(2,0,4)/(4,0,4)`(X축 2m 간격, 고도 4m 고정).
- **cfs-telemetry-app 쪽 spec/코드 주석에 이 제약이 이미 있었으나
  지상 쪽 문서/UI에는 전혀 노출 안 돼 있었음** — GUI에 안내 문구 또는
  클라이언트 사전검증 추가를 고려할 것(현재는 기체가 거부할 때까지
  모름).

## 6. 지상 seq 카운터 데스크 — v1 프로토콜에서 자동복구 불가 (실측 확인)

`_seq_counter`는 기체가 다운링크로 보고하는 `uplink_last_seq`를 보고
자동으로 앞으로 당겨지는 자가복구 로직(`resync_from_device`, BL-03)이
있으나, **이 필드는 DL2(v2) 프레임에만 존재** — v1 텍스트 다운링크
중에는 이 필드가 항상 비어 있어 **자동복구가 원천적으로 작동 불가**.

실측 재현: 지상 서버 재시작(로컬 `_seq_counter`가 1로 리셋) 시점에
기체가 이미 v1으로 seq=7까지 수락한 상태 → 이후 보내는 모든 명령이
seq≤7이라 계속 `uplink proxy rejected replay`로 거부됨. 이번엔 수동으로
seq를 8까지 밀어 올려(같은 명령 5회 재전송) 우회.

**운영 수칙**: 지상 서버를 재시작했는데 명령이 계속 `SEQ_FAIL`
(UFB=2)로 거부되면 — ① 현재 v1/v2 어느 쪽인지 먼저 확인
(`fault_code`/`source` 필드가 FC/SH로 분리돼 있으면 v1) ② v1이면
자동복구가 안 되므로 **CONFIG로 v2 전환**을 먼저 성공시키거나(단, 이
전환 명령 자체도 같은 seq 문제에 걸릴 수 있어 몇 차례 재시도 필요할
수 있음), 수동으로 seq를 여러 번 진행시켜 last_accepted를 넘길 것.

**미해결**: 오늘 v2 전환 시도(CONFIG `downlink_protocol=1`, seq=3)가
seq≤7 구간이라 **실제 적용됐는지 검증 안 됨** — 이후 대화에서 그대로
넘어감. 다음 세션에서 현재 다운링크가 v1/v2 중 뭔지부터 재확인 필요.

## 7. GUI UFB 결과 오탐 — seq 상관관계 없이 매칭 (실측으로 재현, 2026-07-23)

**증상**: GUI에서 CONFIG(seq=11, downlink_protocol=1, force 미체크)를
보냈는데 `[OK] CONFIG accepted` 직후 `[✅ UFB=0] 오류 없이 수신됨`이
떠서 성공한 것처럼 보였음. 그러나 Pi 로그(`journalctl`)에는 해당
seq=11에 대해 `command blocked by health state=1 class=1`만 있고
성공 처리(`config activated`) 이벤트가 전혀 없었음 — **실제로는
계속 차단된 상태인데 GUI는 성공으로 표시**.

**원인**: `uplinkGUI/plugin.js` `onUFBReceived()`(145행)가 **seq
번호로 상관관계를 매칭하지 않고**, WebSocket으로 다음에 들어오는
`uplink_fb` 값을 무조건 "현재 대기 중인 명령의 결과"로 간주함.
이번 재현 케이스: 사용자가 seq=11을 보내 대기하는 사이, **Claude가
백그라운드로 보내고 있던 다른 명령(diagnostic 등)의 정상 응답
(UFB=0)이 먼저 도착** → GUI가 이를 seq=11 결과로 오매칭 → 대기 해제
→ 이후 진짜 seq=11의 UFB=3(STATE_BLOCKED)이 도착했지만 이미
`pendingCommand=null`이라 무시됨.

코드 자체에 이미 이 한계가 문서화돼 있었음(149~153행 주석):
> "UFB_OK는 '정상 처리'와 '보고할 pending 결과 없음(default)'을
> 구분하지 못하는 구조적 한계... UFB=0은 실패가 아니라는 것만 확정,
> '적용됨' 단정은 하지 않는다"

즉 알려진 구조적 한계였으나, **다른 명령과 동시에(또는 그 직후)
명령을 보내면 실제로 오탐이 발생**함을 이번에 실측으로 처음 확인.

**근본 수정 방향(미착수)**: seq 상관관계 매칭 필요 — v2(DL2) 프레임엔
`uplink_last_seq` 필드가 있어 이걸로 정확한 매칭이 가능하지만, 현재
`onUFBReceived()`는 `msg.uplink_fb`만 보고 `msg.uplink_last_seq`는
아예 안 씀. v1 프로토콜에는 이 필드 자체가 없어 v1에서는 구조적으로
매칭이 불가능(프로토콜 한계) — v2 전환이 이 버그의 전제조건이기도 함.

**임시 완화책**: 지상에서 명령을 보낼 때 다른 명령(자동화 스크립트
등)과 동시에 보내지 않을 것 — 이번 세션 한정 회피책, 근본 해결 아님.
