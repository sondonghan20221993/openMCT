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

## 결정

미정 — 기록만 해두고 해결 방식은 추후 결정.

후보:
- A: 서버가 매 RECOVERY 요청마다 랜덤/타임스탬프 기반 0이 아닌 토큰을
  자동 생성해 `Payload[4:8]`에 채워 넣음 (호출자는 action/target/reason만 입력)
- B: GUI에 request_token 입력 필드 추가, 서버는 그대로 전달만
- C: 둘 다 지원(자동 생성 기본값 + 수동 override 옵션)

## 상태

- [ ] 해결 방식 결정 (A/B/C)
- [ ] 구현
- [ ] 검증
