# Ticket 79 — 데이터 사전 점검 이벤트 표시 보완 (2안 경로·RUNNING)

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰 대기. 데모 서버 반영 뒤 실제 화면 확인 남음) — [Issue #316](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/316). 2026-11-07 조율 세션 지시로 착수.

## 배경
웹 데이터 복제 화면과 플랫폼 SSE를 대조한 결과(티켓 77 대조표) 이벤트·키는 연결돼 있지만, 사전 점검 카드 두 곳이 비어 보였다.
1. ADR-007 2안 요청(티켓 76 W9)은 노드에 wg 장치가 없어 `tunnel_interface`를 비운다. `data.preflight.network.path`가 `kind:"other"`, `name:""`로 나갔다.
2. `data.preflight`는 검사가 끝난 뒤 한 번만 나갔다. 검사(최대 수 분) 동안 화면은 "통로 여는 중"에 머물렀다.

## 작업 범위
- 포함: `apps/worker/src/cmp_worker/preflight_events.py`(경로 표시, RUNNING 이벤트), `activities/network_preflight.py`(검사 시작 때 발행)
- 제외: 검사 로직(check_*·probe_*), 판정·시작 규칙, 계약, 웹 저장소, 서버 배포

## 착수 결정 (2026-11-07, Issue #316)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 2안 판단 근거 | `tunnel_interface`가 없고 `tunnel_cidrs`가 있으면 데이터 게이트웨이 경로로 본다 | 2안 요청(`cmp_data.handoff.preflight_request`)은 언제나 이 모양이다(티켓 76 W9). 대안 `tunnel_gateway`를 채우는 것은 `check_route` 판정(다음 홉 대조)을 바꾸므로 범위 밖. 새 계약 필드는 입력 지문이 바뀌어 기각 |
| D2 | 경로 `name` | 원본 주소(`source_db_host`)의 첫 DNS 라벨 = 게이트웨이 Service 이름(`<name>-peer-db`) | 요청에 이미 있는 값만 쓴다. 비밀값 없음 |
| D3 | 경로 `kind`·`cidr` | `wireguard`, `tunnel_cidrs[0]`(기본 `10.210.0.0/24`) | 게이트웨이는 WireGuard다. `wg*` 인터페이스 요청은 기존과 같다. 둘 다 없으면 기존처럼 `other` |
| D4 | RUNNING 시점·발행 위치 | `run_network_preflight` Activity가 검사 전에 한 번(`asyncio.to_thread`) | Workflow 코드는 그대로라 `workflow.patched`가 필요 없다. 발행 실패는 검사를 막지 않는다(E9) |
| D5 | RUNNING 모양 | `status:"RUNNING"`, 네 검사 모두 `PENDING`(code null, evidence ""), `may_start:false`, `checked_at:null`, `input_fingerprint`=요청 지문, `network`·`probe_location`은 요청과 검사 위치로(결과와 같은 값) | 계약(`DataPreflightData`)이 이미 RUNNING·PENDING을 허용한다 → **계약 변경 없음**, `make openapi` 불필요. 웹 목업(`dataScript.ts`)도 같은 모양을 쓴다 |
| D6 | 멱등 키 | `preflight:running:{지문}:{시작 시각}`. 결과 키(`preflight:{지문}:{checked_at}`)와 접두어가 달라 겹치지 않는다 | 같은 입력으로 다시 검사하면 RUNNING이 다시 보여야 한다. Activity 재시도는 시도 번호가 해시에 들어가 다시 낸다 |
| D7 | 이벤트 상태 | RUNNING은 `running`(phase `data_copy`), 결과는 기존대로 `done`/`failed` | 웹 목업과 같다 |

## 수용 기준
- [x] 2안 요청 → `path {kind:"wireguard", name:"<name>-peer-db", cidr:"10.210.0.0/24"}`
- [x] `wg0` 요청 → 기존과 같음, 인터페이스·대역 모두 없음 → `other`
- [x] 검사 전 RUNNING 1회(네 검사 PENDING, `may_start:false`) → 결과 1회, 재검사 때 다시 RUNNING
- [x] 모든 이벤트 웹 계약 검증 통과, RUNNING 저장 실패에도 검사 결과 그대로
- [x] `make check` (3340 통과; 동시 실행 경합으로 실패한 Temporal 시험 2건은 단독 재실행 통과)
- [ ] 데모 서버 반영 뒤 실제 화면 확인

## 시험
`tests/test_preflight_events.py`(새), `tests/test_network_preflight_agent.py`(RUNNING 1건 추가 반영). `make check`.

## 문서
이 티켓, `specs/provisioning/tasks.md`. 계약·OpenAPI 변경 없음. **웹 참고**: `events.ts` 주석 "플랫폼이 보내지 않는 값: 검사·판정의 PENDING·RUNNING"은 이 PR 뒤로 맞지 않는다(`data.preflight` RUNNING과 검사 PENDING을 보낸다).
