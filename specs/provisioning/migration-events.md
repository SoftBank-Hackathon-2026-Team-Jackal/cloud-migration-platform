# 이전 이벤트 저장소·SSE·운영 로그 (티켓 24)

웹 화면(`cloud-migration-web`)이 기대하는 세부 이벤트를 각 파트 담당자가 자기 Activity에서 한 줄로 발행하게 하는 공통 모듈의 명세다. 담당자용 사용법은 [이벤트 발행 안내서](../../docs/engineering/events-guide.md)에 있다.

## 범위

- 이벤트 봉투와 타입별 `data` 계약: `cmp_contracts.events`. 기준은 웹 계약 `src/lib/migration/events.ts`·`contract/events.schema.json`(`Minseo` 브랜치, 커밋 `971ce73`)이며 사본을 [docs/contracts/events.schema.json](../../docs/contracts/events.schema.json)에 둔다.
- 저장: 업무 PostgreSQL `migration_events`(append-only), `migration_logs`.
- 발행: `cmp_worker.events.emit_event`·`log_event`(Activity 전용), `EventEmitter`·`InMemoryEmitter`·`DbEventEmitter`.
- 읽기: `GET /migrations/{id}/events` SSE(snapshot·`Last-Event-ID`·하트비트·종료)와 JSON 읽기(`?after=`), `GET /migrations/{id}/logs?after=`.
- Provisioning 이벤트 연결: `approval.decision`, `plan.ready`, `provision.resource`, `provision.check`.
- PoC 브리지: Finals `poc/demo` 패널 값을 이벤트로 바꾸는 어댑터(`cmp_worker.poc_bridge`).

제외: 다른 담당자의 도메인 로직 수정(분석·샌드박스·데이터·트래픽). 각자 안내서를 보고 자기 Activity에 붙인다.

## 결정

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| E1 이름 | 이벤트 타입·필드·값은 웹 계약 이름을 따른다. 백엔드 이름이 다르면 발행 헬퍼(`cmp_contracts.events.to_web`)가 바꾼다: `check_route→route`(점검 이름 전체), `longest_xact_ms→longest_tx_ms`, `lock_waiters→lock_waits`, `copying/catching_up→copy/catchup`, `image_digest→digest`, `bytes_received→bytes_done`, `bytes_expected→bytes_total`, `seconds→elapsed_ms`(×1000) | 화면이 한 가지 이름만 알면 된다. 담당자는 자기 모델 값을 그대로 넘긴다 |
| E2 봉투 | 저장·JSON 읽기 봉투는 `{migration_id, seq, at, phase, type, status, progress?, data}`. SSE `data:` 줄은 웹 스키마(`additionalProperties: false`)에 맞춰 `migration_id`를 뺀다(스트림이 이미 이전 하나이고, snapshot이 `migration_id`를 담는다) | 웹 스키마는 봉투에 `migration_id`가 없다. 에이전트·보고서는 여러 이전을 다루므로 JSON에는 남긴다 |
| E3 문구 없음 | `message`는 보내지 않는다. `error.message`는 필수라 빈 문자열이다. 값·코드만 보낸다 | 화면이 key·code로 자기 언어 문구를 고른다 |
| E4 비밀값 | `data`·로그 `fields`는 저장 전 `cmp_scanners.masking.mask_text`로 문자열 값을 가린다. 키 이름이 password·secret·token·credential 등이면 값을 통째로 `[MASKED]`로 바꾼다. 비밀값은 참조(Secret name/key)만 보낸다 | PROV-021 |
| E5 크기 | `data` 직렬화 32 KiB, 로그 `fields` 8 KiB 이하. 넘으면 저장하지 않고 운영 로그 `EVENT_TOO_LARGE`를 남긴다 | 큰 산출물은 참조로 전달한다(temporal-flow §12) |
| E6 seq | 이전(`migration_id`)마다 1부터 단조 증가. 트랜잭션 안에서 `max(seq)+1`을 할당하고 `UNIQUE(migration_id, seq)` 충돌이면 다시 시도한다(최대 8회) | PostgreSQL·SQLite(테스트) 모두 같은 코드. 카운터 테이블은 이 규모에서 필요 없다 |
| E7 멱등 | 멱등 키 = sha256(`workflow_id/activity_id/attempt/type/key`). `key`는 `idempotency_key` 인자, 없으면 `data`의 정규 JSON Digest. `UNIQUE(migration_id, idempotency_key)`. 같은 키가 이미 있으면 새로 쓰지 않고 저장된 이벤트를 돌려준다. Activity 밖(API)에서는 `api/-/0/type/key` | 같은 시도 안의 반복 호출은 한 번만 남고, Temporal 재시도(새 attempt)는 새 진행으로 남는다 |
| E8 Activity 전용 | `emit_event`는 Workflow 코드에서 호출하면 `RuntimeError`. Workflow가 이벤트를 내야 하면 `emit_migration_event` Activity(Provisioning 큐)를 실행한다 | Workflow 결정성 |
| E9 실패 격리 | 발행은 관찰이다. 검증·저장 실패는 업무 Activity를 실패시키지 않고 `None`을 돌려주며 운영 로그(`EVENT_INVALID`·`EVENT_STORE_FAILED`)와 Worker 로그를 남긴다 | 이벤트 저장소 장애로 Apply가 멈추면 안 된다 |
| E10 SSE | 연결 직후 `snapshot`. 새 연결은 `reset=true`·지금까지의 이벤트(최대 5,000건), `Last-Event-ID`가 저장된 seq면 `reset=false`·빈 목록(저장소에 없는 id면 새 연결처럼 `reset=true`). snapshot의 `seq`·`id:`는 포함한 마지막 seq. 이후 `seq > 마지막 id` 이벤트를 `id: <seq>`와 함께 보낸다. 0.5초마다 저장소를 읽고, 15초 동안 보낸 것이 없으면 `: heartbeat` 주석을 보낸다 | 웹 `SseProvider`가 `Last-Event-ID`로 이어 받는다 |
| E11 종료 | Workflow 상태가 `done`·`failed`·`cancelled`면 남은 이벤트를 모두 보낸 뒤 닫는다. 상태 대응: `scaffold_complete→done`, `cancelled→cancelled`, `awaiting_approval`·검토 대기→`waiting`, 진행 단계(`cmp_contracts.migration.WEB_PHASE`의 키: `created`·`analysis`·`sandbox`·`build`·`planning_handoff`·`planning`·`workload_planning`·`provisioning`·`data_copy`·`traffic`·`verification`)→`running`, 그 밖의 종료 phase(`failed`·`plan_failed`·`plan_rejected`·`approval_expired`·`provisioning_failed`·Sandbox 종료 상태)→`failed`. 상태는 E19의 실행 상태를 반영한 값이다. Workflow를 조회할 수 없으면 마지막 이벤트로 판단한다 | 기존 SSE는 `failed`에서 닫히지 않았다. 티켓 49(N3)에서 `planning_handoff`·`workload_planning`이 `failed`로 잘못 닫히던 문제를 고쳤다 |
| E12 전환 기간 | 기존 `event: status` 블록을 계속 보낸다(`id:` 없음). `?legacy=false`면 보내지 않는다 | 웹은 `event:` 줄을 무시하고 `data:`를 이벤트로 읽으므로, 웹 전환 뒤 끈다 |
| E13 JSON 읽기 | `Accept: application/json` 또는 `?after=`가 있으면 `{migration_id, events, next_after}`(최대 `limit`건, 기본 500)를 돌려준다. `GET /migrations/{id}/logs?after=`도 같은 모양 | 에이전트(Jackal)·최종 보고서용 |
| E14 운영 로그 | `migration_logs{id, migration_id, at, level, source(worker/activity/api), activity, attempt, code, fields}`. `log_event(migration_id, level, code, fields)` | 이벤트로 설명되지 않는 원인(재시도·실패 코드)을 에이전트가 함께 읽는다 |
| E15 Terraform | AWS·GCP Apply를 `-json`으로 실행하고 `apply_start`·`apply_complete`·`apply_errored` 메시지를 리소스 단위 `provision.resource`로 바꾼다. 진행률은 `planned_change` 수(n/m)로 환산한다. Saved Plan stale 판단은 `diagnostic` 메시지에서 한다 | 리소스마다 시작·완료를 화면에 보인다. 출력 원문은 여전히 결과에 넣지 않는다 |
| E16 Helm | 워크로드 Activity의 heartbeat 문자열(`started`·`running`·`connect`·`helm:<name>`·`ready:<name>`·`pvc`·`lb-*`)은 화면용이 아니라 명령줄 값이다. 이벤트는 단계 경계에서 따로 낸다: `helm:<name>` 시작·완료→`provision.resource`(`key=helm_release`), `pods_ready`·`db_connect`·`app_health` 검사마다 `provision.check` | heartbeat는 Temporal 생존 신호라 문자열을 바꾸지 않는다 |
| E17 phase | 봉투 `phase`는 웹 `BackendPhase`다. Workflow phase → 웹 phase는 `cmp_contracts.migration.WEB_PHASE` 하나로 정한다: `sandbox→build`, `created→analysis`, `planning_handoff`·`workload_planning→planning` | 웹 계약. 표가 둘이면 어긋난다(N3) |
| E18 실패 종료 이벤트 (티켓 49) | `MigrationWorkflow`가 실패로 끝나면(`failed`·`plan_failed`·`provisioning_failed`·`plan_rejected`·`approval_expired`·`data_blocked`·`data_failed`·`traffic_blocked`(티켓 50, 실패 단계 `traffic`), 처리되지 않은 예외) `failure-event-v1` patch 아래 `emit_migration_event`(Workflow 큐에도 등록)로 `type=error`, `status=failed`, `phase=실패 단계`, `data={code, message:"", phase, retryable:false}` 1건과 운영 로그 `MIGRATION_FAILED{outcome, code, phase, issues}`를 남긴다. 멱등 키 `failure:<code>`. 취소는 내지 않는다. 발행이 실패해도 결과는 같다(E9) | #158(34)의 "Workflow 실패 종료 `error`"를 이관했다. 웹 `ErrorData`에 세부 사유 자리가 없어 `issues`는 로그·상태 API에 둔다 |
| E19 실행 상태 반영 (티켓 49) | 상태 API·SSE는 `cmp_api.migration_state.read_migration_state`로 읽는다. Temporal `describe()`가 FAILED·TERMINATED·TIMED_OUT인데 phase가 진행 phase면 `phase=failed`, `failure_code=WORKFLOW_FAILED`·`WORKFLOW_TERMINATED`·`WORKFLOW_TIMED_OUT`(Workflow가 기록한 코드가 있으면 그것), CANCELED면 `cancelled`. 종료된 Workflow의 query는 5초 제한, 실패하면 describe만으로 만든다. 응답에 `execution_status` | 강제 종료·시간 초과는 Workflow 코드가 돌지 않는다 |
| E20 실패 보존 (티켓 49) | `status` query와 `GET /migrations/{id}`에 `failure_code`·`failure_phase`(웹 phase)·`failure_issues`(Plan 게시 `issues` 등 세부 코드)·`plan_code`·`plan`. 예외 메시지는 비밀이 섞일 수 있어 보존하지 않고 `ACTIVITY_FAILED`·`CHILD_WORKFLOW_FAILED`·`WORKFLOW_FAILED` 코드만 둔다. snapshot의 `phase`는 이벤트가 없으면 진행 phase, 실패면 `failure_phase` | D-04: 원인 코드가 사라졌다 |

## 저장 스키마

```text
migration_events(id PK, migration_id, seq, at, phase, type, status, progress NULL, data JSON,
                 idempotency_key(64), UNIQUE(migration_id, seq), UNIQUE(migration_id, idempotency_key))
migration_logs(id PK, migration_id, at, level, source, activity NULL, attempt, code, fields JSON)
```

Alembic revision `20261105_06_migration_events`(down `20261105_05`, 티켓 23 다음).

## 공개 인터페이스

```python
# cmp_worker.events (Activity 안에서만)
emit_event(migration_id, type, phase, status, data, *, progress=None, idempotency_key=None) -> dict | None
await emit_event_async(...)                           # async Activity 본문용(스레드에서 저장)
log_event(migration_id, level, code, fields=None) -> dict | None
configure_emitter(emitter)                            # Worker 시작 시 DbEventEmitter(SessionLocal)

# cmp_api.migration_events
EventEmitter (Protocol: emit(draft, *, actor) / log(entry, *, actor))
InMemoryEmitter, DbEventEmitter(session_factory)
append_event(session, ...), list_events(session, migration_id, after, limit), list_logs(...)
```

## 테스트 Seam

- `InMemoryEmitter`: 이름 변환·검증·마스킹·크기 제한·멱등.
- `DbEventEmitter` + SQLite: seq 단조 증가·동시 할당·멱등 UNIQUE.
- FastAPI `TestClient` + SQLite + 가짜 상태 조회: snapshot·`Last-Event-ID`·하트비트·종료(done/failed/cancelled)·JSON 읽기.
- 계약: `scripts/export_event_schema.py`가 만든 스키마와 웹 스키마 사본의 정규형 비교.
- Provisioning: 가짜 Terraform runner의 `-json` 출력 → `provision.resource`, 가짜 클러스터 → `provision.check`, 승인 API → `approval.decision`, Plan 게시 → `plan.ready`.
