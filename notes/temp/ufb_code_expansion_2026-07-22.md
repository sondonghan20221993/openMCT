# UFB 코드 확장 반영 필요 (cfs-telemetry-app BL-11, 2026-07-22)

## 배경

`cfs-telemetry-app` 저장소 BL-11에서 `lora_tdm_app`의 UFB(Uplink Feedback
Byte)를 4종(`0x00 OK / 0x01 CRC_FAIL / 0x02 SEQ_FAIL / 0x03 STATE_BLOCKED`)
에서 12종(`0x00~0x0B`)으로 확장했다. 나머지 8종은 이전까지 지상에서
전부 "정상"으로 보였던 uplink_app 거부 사유들이다. spec 원본:
`cfs-telemetry-app/notes/lora_tdm_app_behavior_spec.md` §9.2,
`cfs-telemetry-app/notes/lora_protocol_v2_spec.md` (`ufb` 필드 행).

## 확장된 코드표 (기체측 이미 반영, 이 repo는 미반영)

| UFB 값 | 이름 | 의미 |
|---|---|---|
| 0x00 | OK | 정상 수신/pending 없음 |
| 0x01 | CRC_FAIL | 라디오 프레임 CRC 오류 |
| 0x02 | SEQ_FAIL | 시퀀스 거부 |
| 0x03 | STATE_BLOCKED | 헬스 게이트 차단 |
| 0x04 | FAILED | 일반 처리 실패 |
| 0x05 | REJECT_VERSION | 프로토콜 버전 불일치 |
| 0x06 | REJECT_CLASS | 알 수 없는 커맨드 클래스 |
| 0x07 | REJECT_LENGTH | 페이로드 길이 불일치 |
| 0x08 | ROUTE_MISS | 라우팅 대상 없음 |
| 0x09 | REJECT_ROUTE | 라우트 갱신 거부 |
| 0x0A | REJECT_CHECKSUM | 프록시 명령 체크섬 불일치 |
| 0x0B | REJECT_VIEWPOINT | VIEWPOINT 페이로드 거부 |

값 배정은 uplink_app 내부 `UPLINK_APP_Result_t` 번호를 그대로 쓰지 않고
UFB 전용 독립 번호 체계(무선 프로토콜을 SB 내부 enum과 분리) — 상세 근거는
위 spec 문서 참조.

## 이 repo에서 반영이 빠진 지점

1. **`my_openmct_app/src/plugins/uplinkGUI/plugin.js`** (`onUFBReceived()`,
   현재 145~177행 부근) — `ufb === 0/1/2/3`만 분기하는 if/else-if 체인.
   `ufb >= 4`는 아무 분기도 안 타서 `pendingCommand`가 클리어되지 않고
   그대로 타임아웃까지 대기 — 사용자에게 "왜 막혔는지" 안 보임.
   → 0x04~0x0B 8개 분기 추가, 각 UFB 값에 맞는 로그 메시지(위 표의 "의미"
   활용) + `clearPendingCommand()` 호출 필요.
2. **`lora_protocol_v2.py`** — UFB는 raw int로만 패스스루(`ufb: int`,
   `frame.ufb`), 이름 변환 레이어 자체가 없음. 새 코드가 추가돼도 파싱
   자체는 깨지지 않지만(그냥 정수), 사람이 읽을 이름 매핑이 있으면 로그/
   WS 메시지 디버깅에 유리 — 선택사항(필수 아님, 현재 값 그대로 통과돼도
   기능은 동작함).
3. **`cfs-telemetry-app/bridge/lora_downlink_decoder.py`** (자매 파일,
   `dl2_downlink_integration.md` 참고 — 두 파일이 동기화 대상으로 이미
   관리되고 있음) — 여기도 UFB 이름 매핑이 있다면 동일하게 갱신 필요,
   없다면 2번과 동일하게 선택사항.

## 우선순위

**1번(plugin.js)이 실사용 영향 있음** — 운용자가 명령 실패 사유를 못 보고
막연히 기다리게 됨. 2/3번은 로그 가독성 개선이라 우선순위 낮음.

## 상태

- [x] **완료(2026-07-22)**: plugin.js `onUFBReceived()`에 0x04~0x0B 분기 추가.
      추가로 `cfs-telemetry-app` BL-CTR(counter management, 같은 날 후속
      작업)에서 신설된 0x0C(`REJECT_COUNTER`)도 같이 반영 — 이 문서의 표는
      0x0B까지만 다루지만 실제 코드는 0x0C까지 처리한다.
      기존 uncommitted 변경(UFB=0 문구 수정 + UFB=3 분기, 이 문서 작성
      시점에 이미 M으로 잡혀 있던 것)도 같은 함수 블록이라 이번 커밋에
      함께 포함.
- [ ] (선택, 미착수) lora_protocol_v2.py / lora_downlink_decoder.py에
      이름 매핑 헬퍼 추가 — 필수 아님, 현재 값 그대로 통과돼도 기능 동작함
