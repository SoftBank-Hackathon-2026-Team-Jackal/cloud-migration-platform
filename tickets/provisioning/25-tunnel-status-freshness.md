# Ticket 25 — 터널 상태 만료(신선도) 처리

## 상태
DONE (로컬 범위, 조율 세션 리뷰·push 대기) — [Issue #135](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/135). 2026-11-05 조율 세션 지시로 착수 결정을 기록하고 구현했다. 2026-11-06 실제 중계 서버 합동 시험 전에 닫아야 할 안전 공백이다.

## 목적
16(#70)·17(#71)에서 ON_PREM 인계는 Agent가 보고한 최신 터널 상태(`agent_tunnel_status`)를 기한 없이 믿는다. Agent가 "앱 헬스 OK·DB 접속 OK"를 보고한 뒤 죽거나 터널이 끊기면 Traffic·Data 인계가 여전히 진행 가능으로 보여, 죽은 대상으로 트래픽을 보내거나 복제를 시작할 수 있다. 오래된 보고는 "지금 확인됨"의 근거로 쓰지 않는다.

## 관련 요구사항
PROV-009, PROV-019, PROV-020, PROV-021

## 선행 작업
Blocked by: 13, 16, 17, 23 (DONE)

## 현재 구현과 재사용 지점
- `cmp_api.tunnels.TunnelStatus`(16): `tunnel_ip`·`last_handshake_at`(Agent가 읽은 WireGuard handshake epoch)·`app_health_ok`·`db_connect_ok`·`code`(17 W8)·`reported_at`(API 수신 시각)
- `cmp_api.tunnels.tunnel_view`(16 T6): 인계 뷰에 합칠 최신 터널 상태
- `cmp_providers.provisioning_result.handoff`(13 R7·16 T2/T7/T11): ON_PREM `external_*`·`tunnel_endpoint` 채움과 `blocked_by`
- `cmp_worker.activities.approval.ApprovalActivities._handoff`: `get_migration_handoff` Activity
- `cmp_api.connections.agent_summaries`(23): `GET /connections`·`GET /agent-admin/agents`의 `agents[].tunnel`
- 17 W6·W7: Agent는 15초마다 보고하고 handshake가 180초(WireGuard Reject-After-Time) 넘게 없으면 끊긴 것으로 본다

## 범위
- 포함
  - 터널 보고 신선도 판정(설정 가능한 만료 시간, 시각 주입)
  - 인계 뷰: 오래된 보고면 ON_PREM `application.external_*`·`database.tunnel_endpoint`를 채우지 않고 구조화 차단 코드
  - 통합 목록(`GET /connections`, `GET /agent-admin/agents`)의 `tunnel.stale`·`tunnel.fresh_until` 표시 필드(추가만)
  - SDD·OpenAPI 갱신
- 제외: Agent 쪽 보고 주기 변경, 중계 서버 쪽 peer 정리, 폐기 Agent 목록 정리(아래 후속 메모), DB 스키마 변경, Traffic·Data 코드

## 수용 기준 (Issue #135)
- [x] 마지막 보고가 만료 시간보다 오래되면 ON_PREM Traffic·Data 인계가 막히고 차단 코드가 남는다.
- [x] 새 보고가 오면 다시 진행 가능해진다.
- [x] 저장된 결과는 바꾸지 않는다(16 T6 유지).
- [x] 만료 기준이 설정값이고 기본값 근거가 기록된다.

## Test Seam
- 인계: SQLite 업무 DB + 실제 터널 API(`PUT .../tunnel/key`, `POST .../tunnel/status`) + 실제 `get_migration_handoff` Activity(`ApprovalActivities(session_factory, clock=...)`로 시각 주입)
- 목록: `cmp_api.connections.agent_summaries(session, now=...)` 순수 경계와 `GET /connections`·`GET /agent-admin/agents` HTTP 응답
- 놓치는 것: 실제 Agent·중계 서버의 보고 지연·시계 차이(2026-11-06 합동 시험)

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| F1 신선도 규칙 | 보고는 다음 두 조건을 **모두** 만족할 때만 신선(fresh)하다. ① `now - reported_at <= TUNNEL_STATUS_TTL_SECONDS` ② `last_handshake_at`이 있고 `now - last_handshake_at <= TUNNEL_HANDSHAKE_STALE_SECONDS`. 아니면 `stale`이다. `fresh_until = min(reported_at + TTL, last_handshake_at + HANDSHAKE)`(handshake가 없으면 `null`) | ①은 Agent·API 생존(보고가 끊김), ②는 터널 생존(Agent는 살아 있으나 보고 내용이 오래된 handshake에 기대는 경우)을 본다. ②는 Agent 시계 기준 값이라 시계 차이만큼 오차가 있다(미검증 참조) |
| F2 기본값 | `TUNNEL_STATUS_TTL_SECONDS=60`, `TUNNEL_HANDSHAKE_STALE_SECONDS=180`. 둘 다 `cmp_api.config.Settings` 환경 변수로 바꿀 수 있다(API·Worker 공통) | 60초 = 15초 보고 4회 연속 누락. 한두 번의 일시적 실패(API 재시작·네트워크 순간 끊김)로 인계가 흔들리지 않으면서 1분 안에 죽은 대상을 막는다. 23의 Agent `online` 기준(`ONLINE_SECONDS=60`)과 같아 화면 표시가 어긋나지 않는다. 180초는 17 W6과 WireGuard Reject-After-Time(180초) — 그 뒤로는 WireGuard가 그 세션으로 데이터를 보내지 않는다 |
| F3 차단 코드 | 새 `blocked_by` 코드 `TUNNEL_STATUS_STALE`(Traffic·Data 공통). 해당 consumer의 확인 항목이 보고상 성공(`app_health_ok`/`db_connect_ok` = true)인데 보고가 stale일 때만 쓴다. 보고 없음·보고상 실패는 기존 코드 그대로(Data `TUNNEL_DB_NOT_VERIFIED`, Traffic `EXTERNAL_ENDPOINT_UNAVAILABLE`) | 한 코드는 한 뜻: 기존 코드 = "확인 안 됨/실패", 새 코드 = "확인됐었지만 근거가 만료됨". 현재 Traffic·Data 코드는 `proceed`만 본다(`blocked_by` 사용처 없음 확인) |
| F4 시각 출처 | API 서버 시계(`int(datetime.now(UTC).timestamp())`). `reported_at`도 같은 시계로 저장된다. 판정 함수는 `now`를 인자로 받고, `ApprovalActivities(..., clock=)`·`agent_summaries(..., now=)`로 테스트에서 주입한다 | Workflow 안이 아니라 Activity 안의 읽기 시점 판정이라 결정론 제약이 없다. 같은 시계라 ①은 시계 차이가 없다 |
| F5 인계 뷰 | `tunnel_view`가 `stale`·`fresh_until`을 함께 돌려주고, `handoff`는 stale이면 ON_PREM `external_endpoint`·`external_host`·`external_port`·`health_path`(터널값)·`tunnel_endpoint`를 채우지 않는다. 저장된 결과는 바꾸지 않는다(16 T6). 새 보고가 오면 다음 읽기에서 다시 진행된다. AWS/GCP는 터널 상태를 읽지 않으므로 영향 없음 | 판정은 읽는 시점마다 다시 한다(저장 없음, DB 변경 없음) |
| F6 목록 표시 | `agents[].tunnel`에 `stale: bool`, `fresh_until: int \| null`을 **추가**한다. 기존 `tunnel_ip`·`last_handshake_at`·`app_health_ok`·`db_connect_ok`은 이름·뜻 그대로. `online`은 23 정의 그대로(마지막 접속 60초) | 웹 계약 mock과의 호환(추가만). 화면은 `stale`이면 앱/DB OK 표시를 "확인 만료"로 보일 수 있다 |
| F7 DB | 스키마 변경 없음 | `reported_at`·`last_handshake_at`이 이미 저장된다 |

### 후속 메모 (구현하지 않음)
- 폐기(`revoked`)된 Agent는 중계 서버용 목록(`GET /relays/{relay_id}/agents`)에 `revoked: true`로 영구히 남는다(16 T12). 중계 서버가 peer를 지운 뒤에도 목록이 계속 커진다. 폐기 후 일정 기간(예: 7일)이 지나면 목록에서 빼는 정리 규칙은 별도 티켓으로 둔다. 빼기 전에 모든 중계 서버가 반영했는지 알 방법(`list_version` 확인 보고)이 없다는 점을 함께 정해야 한다.

## 구현 요약
- 설정: `cmp_api.config.Settings.tunnel_status_ttl_seconds`(60)·`tunnel_handshake_stale_seconds`(180), 환경 변수 `TUNNEL_STATUS_TTL_SECONDS`·`TUNNEL_HANDSHAKE_STALE_SECONDS`
- 판정: `cmp_api.tunnels.tunnel_freshness(status, now=) -> (stale, fresh_until)`. `tunnel_view(session, agent_id, now=)`가 `stale`·`fresh_until`을 함께 돌려준다
- 인계: `cmp_providers.provisioning_result.handoff`가 확인 성공 + `stale`이면 터널 값을 채우지 않고 `TUNNEL_STATUS_STALE`. `ApprovalActivities(session_factory, clock=None)`이 읽는 시점의 `now`를 넘긴다(기본 API 서버 시계)
- 목록: `cmp_api.connections.agent_summaries`와 `TunnelSummary`에 `stale`·`fresh_until` 추가(기존 필드 그대로)
- DB 스키마·Workflow·Agent·`agent_registry.py`·`routes/agents.py`·Traffic/Data 코드 변경 없음

## 검증 기록 (2026-11-05)
- `make check`: ruff lint/format 통과, `pytest` 1547 passed, 8 skipped
- `tests/test_tunnel_exchange.py` 신규 7개(실제 터널 API + 실제 `get_migration_handoff` Activity, 시각 주입): 신선한 보고 Data·Traffic 진행(TTL 경계 60초 포함), 보고 61초 경과 시 두 인계 모두 `TUNNEL_STATUS_STALE`·터널 값 없음·저장 결과 불변, 보고는 최신이나 handshake 181초 경과 시 차단, 새 보고 후 다시 진행, 보고상 실패는 만료돼도 기존 코드 유지, TTL 설정값 반영, `tunnel_freshness` 경계값(`fresh_until` = 두 기한의 최솟값, handshake 없음 = stale)
- 기존 16 테스트의 보고 helper가 고정 handshake 시각(1793836800) 대신 현재 시각을 보내도록 바꿨다(고정값은 이제 180초 규칙상 stale)
- `tests/test_environment_connections.py`: 웹 계약 테스트에 `stale: false`·`fresh_until` 추가, 신규 1개(목록 `stale` 경계, handshake 오래됨·없음, `online`은 23 규칙 그대로)
- AWS/GCP 무영향: `handoff`는 ON_PREM일 때만 터널 상태를 읽는다. 기존 13 `test_provisioning_result`(GCP·AWS 인계 진행)·22 `test_cloud_workload_plan`(AWS·GCP DATA/TRAFFIC `proceed`) 회귀 통과
- `make openapi`: `TunnelSummary`에 `stale`(boolean, 필수)·`fresh_until`(integer|null, 필수) 추가만 있는 diff 검토
- `alembic check`: DB 스키마 변경이 없어 실행하지 않았다(기존 `test_alembic_migration_matches_tunnel_models` 통과)

## 미검증
- `make smoke-temporal`: 로컬 Temporal(7233)이 꺼져 있어 실행하지 못했다. Workflow 코드는 바뀌지 않았고 Activity 내부 판정만 바뀌었다
- 실제 Agent(v0.2.0)·실제 중계 서버의 보고 지연과 시계 차이: `last_handshake_at`은 Agent(노트북) 시계, `reported_at`·`now`는 API 서버 시계다. 노트북 시계가 크게 틀리면 handshake 판정이 어긋난다(앞서면 늦게, 뒤처지면 일찍 stale). 2026-11-06 합동 시험에서 NTP 동기 상태와 `fresh_until` 표시를 함께 확인한다
- 웹 화면의 `stale` 표시는 웹 담당 범위(필드만 제공)

## 관련 문서
- [13](13-result-handoff.md) · [16](16-tunnel-config-exchange.md) · [17](17-agent-wireguard-tunnel.md) · [23](23-environment-connections.md) · [공유 계약](../../specs/provisioning/shared-contracts.md) · [온프레미스 공통 배포](../../specs/provisioning/onprem-shared-deployment.md)

## 설계 변경 기록
- 2026-11-05: 착수 결정 F1~F7. 인계 차단 코드 `TUNNEL_STATUS_STALE` 추가, 목록 `tunnel`에 필드 추가. spec.md 요구사항 변경 없음

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영 (shared-contracts·onprem-shared-deployment·13·16·OpenAPI, ADR 변경 없음)
