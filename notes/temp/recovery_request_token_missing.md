# RECOVERY 명령 request_token 자동화 누락 (2026-07-14 도출)

## 문제

`fc_serial_ws_server.py::_handle_recovery()`가 요구 인증레벨(3)에 필요한
`request_token`을 자동으로 생성/삽입하지 않는다.

- 기체측 `uplink_app_utils.c::ForwardRecoveryCommand()`는
  `Cmd->Payload[4:8]`(리틀엔디언 uint32)을 `RequestToken`으로 파싱한다.
- `uplink_app_cmds.c::IsAuthorized()`는 `required_level == 3`이면
  `RequestToken == 0`인 경우 무조건 거부한다(§18.11.1).
- `_handle_recovery(body)`는 `body["payload_hex"]`를 검증 없이 그대로
  `bytes.fromhex()`해서 payload로 씀 — 호출자(GUI/사용자)가 직접
  8바이트 이상, 그중 `[4:8]`에 0이 아닌 토큰을 손수 넣어야만 통과한다.
  자동 생성 로직이 전혀 없다.

## 왜 문제인가

- 2026-07-14에 `_auth_level_flag_bits()`를 `_handle_route`/`_handle_recovery`
  양쪽에 적용해 flags 비트[7:6] 문제는 해결했지만(commit `f65b295`),
  RECOVERY는 flags만으로는 부족 — token 없이는 여전히 거부된다.
- GUI에서 RECOVERY 버튼을 그냥 누르면(payload_hex 미입력) `payload = b""`가
  되어 `Cmd->PayloadLength < 8U`이라 `ForwardRecoveryCommand`가
  `RequestToken`을 아예 0으로 남김(memset 초기값) → 항상 거부.
- 즉 RECOVERY 명령은 현재 UI 경로로는 사실상 쓸 수 없는 상태 —
  API를 직접 호출해서 8바이트 이상 payload_hex를 수동 조립해야만 동작.

## 결정 (2026-07-14)

**A안 채택** — 서버가 매 RECOVERY 요청마다 0이 아닌 토큰을 자동 생성해
`Payload[4:8]`에 채워 넣는다. 호출자(GUI/API)는 action/target/reason(첫 4바이트)만
입력하면 되고, token은 신경 쓸 필요 없음 — 즉시 사용 가능하게 만드는 게 목적이므로
수동 입력 필드(B)는 불필요한 마찰. C는 지금 시점에 과설계.

### 구현 범위

`fc_serial_ws_server.py::_handle_recovery()`:
- `payload_hex`로 들어온 앞 4바이트(action/target/reason)는 그대로 유지 —
  4바이트 미만이면 0으로 패딩.
- 뒤 4바이트(`[4:8]`, RequestToken)는 클라이언트 입력을 무시하고 서버가
  `random.getrandbits(32)`(0이면 1로 대체)로 생성해 덮어씀.
- 응답 JSON에 `request_token`을 포함해 로그/디버깅에서 확인 가능하게 함.

페이로드 레이아웃 근거(`uplink_app_utils.c::UPLINK_APP_ForwardRecoveryCommand`,
`uplink_app/config/default_uplink_app_msgstruct.h` `UPLINK_APP_RecoveryCmdTlm_t`):
`[0]`=RecoveryAction, `[1]`=TargetComponent, `[2:4]`=ReasonCode(u16 LE),
`[4:8]`=RequestToken(u32 LE). `PayloadLength < 8`이면 필드가 0으로 남는다
(§18.11.1 레벨3 게이트에서 토큰 0은 항상 거부).

## 상태

- [x] 해결 방식 결정 (A안)
- [x] 구현 — `fc_serial_ws_server.py::_handle_recovery()`, `_generate_request_token()` 추가
- [x] 검증 — 모듈 로드 후 토큰 생성/페이로드 조립/프레임 빌드 수동 확인
      (payload=`01020300059A1483`, flags=`0xC0`, token 0이 아님 확인). 실기체 왕복
      테스트는 Pi 배포 후 별도.
