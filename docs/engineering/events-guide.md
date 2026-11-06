# 이전 이벤트 발행 안내서 (담당자용)

웹 화면은 `GET /migrations/{id}/events` SSE로 이벤트를 받아 움직인다. 지금은 목업 이벤트로 돈다. 각 파트가 자기 Activity에서 아래 이벤트를 **한 줄로** 발행하면 같은 화면이 실제 값으로 움직인다. 명세: [migration-events.md](../../specs/provisioning/migration-events.md). 웹 계약 사본: [events.schema.json](../contracts/events.schema.json).

## 1. 쓰는 법

```python
from cmp_worker.events import emit_event, emit_event_async, log_event

# 동기 코드(asyncio.to_thread 안 포함)
emit_event(
    migration_id,
    "provision.check",
    "provisioning",
    "running",
    {"name": "pods_ready", "state": "done"},
)

# async Activity 본문
await emit_event_async(migration_id, "data.phase", "data_copy", "running", {"phase": "copying"})

# 이벤트로 설명되지 않는 원인(재시도, 실패 코드)은 운영 로그로
log_event(migration_id, "error", "SBX_APPLY_FAILED", {"attempt": 2, "namespace": ns})
```

규칙:

- **Activity 안에서만** 부른다. Workflow 코드에서 부르면 `RuntimeError`다(결정성). Workflow가 꼭 내야 하면 `emit_migration_event` Activity(Provisioning 큐)를 실행한다: `{migration_id, type, phase, status, data, idempotency_key?}`.
- `migration_id`는 부모 `MigrationWorkflow`의 Workflow ID(`migration-<uuid>`)다. 자식 Workflow(`analysis-…`, `sandbox-…`)에서는 입력의 `migration_id`를 쓴다.
- `type`·`data` 필드는 **웹 이름**이다. 아래 이름은 자동으로 바뀌니 기존 모델 값을 그대로 넘겨도 된다.

  | 백엔드 이름 | 웹 이름 |
  | --- | --- |
  | `check_cidr_overlap`·`check_route`·`probe_db_path`·`probe_large_transfer` (점검 `name`) | `cidr_overlap`·`route`·`db_path`·`large_transfer` |
  | `longest_xact_ms`, `lock_waiters` | `longest_tx_ms`, `lock_waits` |
  | `data.phase`의 `copying`, `catching_up` | `copy`, `catchup` |
  | `image_digest` | `digest` |
  | `bytes_received`, `bytes_expected`, `seconds` | `bytes_done`, `bytes_total`, `elapsed_ms`(×1000) |

- `phase`는 웹 단계 이름: `analysis` · `build`(샌드박스·빌드) · `planning` · `awaiting_approval` · `provisioning` · `data_copy` · `traffic` · `verification`.
- `status`는 `running` · `done` · `waiting` · `failed`. **`failed`는 화면에서 이전 실패로 보인다.** 재시도할 실패(빌드 1차 실패 등)는 `running`으로 보내고 `data`의 상태 값으로 표현한다.
- **문구는 보내지 않는다.** 값·코드·식별자만 보낸다(화면이 key로 자기 언어 문구를 고른다). `error.message`는 빈 문자열.
- **비밀값은 참조만**(Secret name/key). 저장 전 `cmp_scanners.masking`으로 한 번 더 가리고, 키 이름이 password·secret·token·credential이면 값을 통째로 가린다(`*_ref`는 유지).
- 같은 Activity 시도 안에서 같은 타입을 여러 번 내면 `idempotency_key`로 구분한다(기본값은 `data` Digest라 같은 값은 한 번만 남는다). Temporal 재시도(새 attempt)는 새 이벤트로 남는다(`across_retries=True`면 같은 키는 재시도에도 한 건, Provisioning 59).
- `data`는 32 KiB 이하. 틀린 필드·이름·큰 데이터는 저장하지 않고 `None`을 돌려주며 운영 로그 `EVENT_INVALID`·`EVENT_TOO_LARGE`를 남긴다. 발행 실패가 Activity를 실패시키지는 않는다.
- 테스트: `configure_emitter(InMemoryEmitter())`로 바꾸고 `emitter.of_type("…")`로 확인한다([tests/test_migration_events.py](../../tests/test_migration_events.py) 참고).

## 2. 파트별 발행표

표의 "기존 값"은 지금 코드에 있는 모델·반환값이다. `[없음]`은 아직 값을 만드는 코드가 없다는 뜻이다(§5).

### 분석 (승철님, **연결 완료** #226) — `phase: analysis`

`cmp_worker.analysis_events`가 실제 분석 Activity(`AnalysisActivities`) 끝에서 낸다. 스캔 안의 항목 단위 진행 신호는 없어서, 스캔이 끝날 때마다 개수를 묶어 한 건씩 낸다. 웹 기본 분석 화면(`AnalysisStage`)이 이 `analysis.scan`을 그린다. `analysis.finding`은 웹의 예전 방식(`?analysis=items`)이라 내지 않는다. 같은 item 키로 `analysis.finding`을 내면 웹에 `analysis.u.<key>` 문구가 없어서 화면에 키 이름이 그대로 나온다.

| 보낼 이벤트 | 어느 Activity에서 | `stream`·n/m | `items` (웹 `analysis.item.<key>`, 개수만) | `evidence` |
| --- | --- | --- | --- | --- |
| `analysis.scan` | `fetch_source` 끝 | code 1/3 | `files`(작업 공간 파일 수, `.git` 등 제외), `commit` | `CODE` `host/owner/repo@sha7`(scheme·사용자 정보·토큰 제거) |
| `analysis.scan` | `scan_manifests` 끝 | code 2/3 | `manifests`(워크로드가 있는 파일 수), `services`, `statefulsets`, `health_endpoint`(probe가 있으면 1) | `MANIFEST` 파일:줄 |
| `analysis.scan` | `scan_code` 끝 | code 3/3 | `db_driver`(`sku`=드라이버 이름), `s3_sdk`, `env_keys`(읽는 키 이름 수) | `CODE` 파일:줄 |
| `analysis.scan` | `scan_aws_infra` 끝 | infrastructure 1/2 | `resources`, `eks_cluster`, `alb`, `iam_role`, `trail_calls`. 미수집·실패면 `[]` | `CLOUD_RESOURCE`·`IAM_POLICY`(`role/<이름>`, 계정 ID 없음)·`CLOUDTRAIL`, 늘 `verified: false` |
| `analysis.scan` (`activity: scan_network`) | `build_requirements` 끝(네트워크 분석 `build_network`가 여기서 돈다) | infrastructure 2/2 | `vpc`, `subnet`, `security_group`(AWS 스캔이 있을 때) | `CLOUD_RESOURCE` VPC, `CONFIG` 외부 진입 |
| `analysis.done` | `build_requirements`가 요구사항을 만든 직후(막혔으면 내지 않음) | — | `{}` (status `done`) | — |

- `data.id`는 `scan-<activity>`로 고정이다. 웹은 같은 id를 덮어쓰고, 흐름별로 가장 큰 `completed`를 진행률로 쓴다. 그래서 병렬 스캔이 어떤 순서로 끝나든, Temporal이 재시도해 새 attempt가 새 행으로 남든 화면이 중복되거나 뒤로 가지 않는다.
- 웹 계약에 있는 `node`·`nat_gateway`·`ebs_gb`·`s3_bucket`은 스캐너가 아직 수집하지 않아 내지 않는다.
- 요약에 실패해도 스캔은 성공으로 끝나고, 운영 로그 `ANALYSIS_EVENT_FAILED`만 남는다.
- `migration_id`는 분석 입력값이다. 웹 시작 경로와 CLI 시작 경로는 이 칸에 `project_id`를 넣지만, `MigrationWorkflow`가 자식을 시작할 때 자기 Workflow ID(`migration-<uuid>`)로 덮어쓴다. 이 동작은 `tests/analysis/test_scan_events_workflow.py`가 확인한다.
- scaffold 분석(문자열 입력)은 실제 스캔을 하지 않으므로 내지 않는다.

### 샌드박스·빌드 (승철님, 발행은 Provisioning 59 대행) — `phase: build`

발행 위치(Provisioning 59, [티켓](../../tickets/provisioning/59-sandbox-events.md)): `SandboxVerificationWorkflow._run_integrated`는 패치 `sandbox-events-v1` 뒤로 각 `sbx_*` Activity 입력에 `events: {migration_id, run, attempt}`를 넣고, Activity가 자기 단계 이벤트를 낸다(문맥이 없으면 내지 않는다). Workflow 수준 이벤트(`sandbox.attempt`·`sandbox.done`·`build.review`)는 샌드박스 큐의 `sbx_emit` Activity로 낸다(실패해도 루프는 그대로). `run`은 `sbx_apply` 횟수(화면 트랙, INFRA 재시도는 run만 늘린다), `attempt`는 수정 시도(1~3). 키는 `sbx:{run}:{attempt}:{step}:{state}`이고 `across_retries=True`라 Activity 재시도에도 한 건만 남는다. 도우미는 `cmp_worker.sandbox_events`.

| 보낼 이벤트 | 어디서 | 값 | 예시 |
| --- | --- | --- | --- |
| `build.check` | `sbx_build` 시작·끝 | `{id:"build", state, attempt}` | `{"id":"build","state":"done","attempt":1}` |
| `build.result` | `sbx_build` 끝(시도마다) | 첫 앱 이미지의 `digest`·`image_uri`, 증거 경로의 `reused`, 실패 코드(계약 밖이면 `BUILD_FAILED`) | `{"attempt":1,"max_attempts":3,"status":"SUCCEEDED","digest":"sha256:…","image_uri":"…/app@sha256:…","reused":false,"error_code":null}` |
| `sandbox.env` | 첫 `sbx_prepare` 성공 | `plan.namespace`, `plan.expires_at - now`(분), `allowlist(network_intent, keep_hosts)` 길이 | `{"namespace":"sbx-m1-r1","expires_minutes":120,"egress_allowed":3}` |
| `sandbox.step` | `sbx_apply`(render·apply·ready), `sbx_verify`(test·judge) | 단계 running → done/failed. 판정 실패는 judge failed | `{"run":1,"attempt":1,"step":"apply","state":"done"}` |
| `sandbox.tests` | `sbx_verify` 시작(테스트 실행 전) | 고정 묶음의 출처별 개수(STANDARD→catalog, ORIGINAL→original, AI→generated), `READ_ONLY` 수 | `{"total":12,"catalog":6,"original":4,"generated":2,"read_only":3}` |
| `sandbox.attempt` | Workflow(판정 직후, 수정이 있으면 같은 run을 `fix_ids`와 다시) | `verdict`, 분류, `sbx_verify`가 돌려준 `checks` | `{"run":1,"attempt":1,"max_attempts":3,"verdict":{"passed":false,"reasons":["TEST_FAILED:std-health"]},"failure_class":"CODE","checks":{…},"fix_ids":["fix-1-1"]}` |
| `build.fix` | `sbx_fix` 끝(고친 파일마다, 요구사항 수정은 한 건) | 바뀐 줄(가림, `.env` 계열은 키만), `why`=분류 사유 코드, `evidence`=파일 | `{"id":"fix-1-1","check":"code","lines":[{"kind":"add","text":"+ HOST = '0.0.0.0'"}],"why":"TEST_FAILED:std-health","evidence":"app.py","file":"app.py"}` |
| `build.review` | Workflow(3회 실패로 `NEEDS_REVIEW`, `sandbox.done` 뒤) | 시도 수 | `{"reason":"max_attempts","attempts":3}` (status `waiting`) |
| `sandbox.done` | Workflow(정리 전 `PENDING`, 정리 뒤 `CLEANED`) | 루프 결과, 통과 Digest | `{"status":"PASSED","cleanup_status":"CLEANED","digest":"sha256:…"}` (status `done`, 통과 외 `failed`) |

### 계획: 매핑 (승철님) — `phase: planning`

| 보낼 이벤트 | 어느 Activity에서 | 기존 값 → 필드 | 예시 |
| --- | --- | --- | --- |
| `plan.step` | `map_usage`·`check_evidence` 끝(행마다) | `MappingRow`: `id`→`id`, `grade`→`grade`, `treatment`→`action`(없으면 `APPROVAL`), `disposition == "KEEP"`→`keep_source: true`, `status`→`verification`, `evidence[0].kind == CODE`면 `group: code` 아니면 `infrastructure`, `capability`→`before`, `evidence[].locator`(쉼표로) →`evidence`, `usage`→`change`, `after`는 대상 서비스 값이 없어 `""` | `{"id":"row-s3","group":"code","grade":"API_DIFF","before":"s3","after":"","evidence":"app/storage.py:12","change":"put_object","action":"PROCEED","keep_source":false,"verification":"EVIDENCE_CHECKED"}` |

### 계획 게시·승인·구축 (민서, **연결 완료**)

| 이벤트 | 발행 위치 | 값 |
| --- | --- | --- |
| `plan.ready` | `publish_migration_plan`·`publish_workload_plan` Activity(게시 성공 시, `PlanReadyEvents` 래퍼). AWS/GCP는 인프라 Plan과 워크로드 Plan에서 한 번씩 나오며, 화면은 마지막 값을 승인 대상으로 쓴다 | `{plan_id, revision, manifest_digest, preview_digest}` — 승인 API에 그대로 되돌려 보낸다 |
| `approval.decision` | 승인 API `POST …/approvals`, 재전달 `POST …/delivery` | `ApprovalResponse`와 같은 값(승인자 ID 제외). `DELIVERED`면 `done`, `PENDING`·`FAILED`면 `waiting` |
| `error` (Migration 실패 종료) | `MigrationWorkflow`가 실패로 끝날 때 공통으로 1건(티켓 49, 각 파트가 따로 낼 필요 없음) | `{code, message:"", phase, retryable:false}`. code는 Workflow 실패 코드(`PLAN_FAILED`·`PLANNING_INPUT_MISSING`·`STOPPED_RENDER`·`ACTIVITY_FAILED` 등), 세부 코드(`issues`)는 운영 로그 `MIGRATION_FAILED`·상태 API `failure_issues` |
| `provision.resource` | `execute_aws_plan`·`execute_gcp_plan`(Terraform `apply -json`: 리소스마다 시작·완료·오류, `key` = `network`/`cluster`/`iam`/`other`, `name` = 주소), `execute_aws_workloads`·`execute_gcp_workloads`(Helm release `db`·`app`, 외부 LB `lb`, NetworkPolicy 적용 `network`) | `{"id":"aws_vpc.target","key":"network","state":"done","progress":50,"name":"aws_vpc.target"}`. 진행률은 n/m(Terraform: `planned_change` 수 또는 Preview 변경 수, 워크로드: 7단계) |
| `provision.check` | 워크로드 Activity 검사마다 | `pods_ready`(DB·앱 각각), `db_connect`, `app_health` → `{"name":"db_connect","state":"done"}` |

Helm heartbeat 문자열(`started`·`running`·`connect`·`helm:<name>`)은 Temporal 생존 신호(명령줄 값)로 그대로 두고, 이벤트는 위처럼 단계 경계에서 따로 낸다.

### 데이터 이전 (데이터 담당) — `phase: data_copy`

플랫폼 Activity(`copy_data`)는 아직 스캐폴드다. 그 전까지는 §3 PoC 브리지가 같은 이벤트를 낸다. 플랫폼 구현 때는 아래 위치에서 같은 값을 낸다.

| 보낼 이벤트 | 어느 Activity에서 | 기존 값 → 필드 | 예시 |
| --- | --- | --- | --- |
| `data.preflight` | `run_network_preflight` 끝(매번 전체). **Activity가 직접 낸다**(MNP-01 N3, `cmp_worker.preflight_events`) — 데이터 담당이 따로 낼 필요 없음 | `NetworkPreflightResult`: `status`, `checks[{name,status,code,evidence}]`(이름·`bytes_received`·`bytes_expected`·`seconds` 자동 변환), `probe_location`, `input_fingerprint`, `checked_at`, `may_start` = `may_start_data_migration(...)`. `network`(CIDR·경로·DB 주소)는 요청값에서 채운다. `evidence`는 원문 문자열(비밀 없음), `mtu`는 지금 항상 `NOT_ATTEMPTED` | `{"status":"PASS","checks":[{"name":"check_route","status":"PASS","code":null,"evidence":"dev wg0"}],…}` |
| `data.phase` | 단계가 바뀔 때 | `preflight`·`blocked`·`schema`·`copying`→`copy`·`catching_up`→`catchup`·`streaming` | `{"phase":"copying"}` |
| `data.lsn` | 1초 주기 | `pg_current_wal_lsn()` → `source_lsn`, 대상 replay LSN → `target_lsn`, 슬롯 지연 → `lag_bytes`, `lag_ms`(없으면 null) | `{"source_lsn":"0/3000148","target_lsn":"0/3000100","lag_bytes":72,"lag_ms":null}` |
| `data.table` | 테이블 상태가 바뀔 때 | `pg_subscription_rel.srsubstate`(i→waiting, d→copying, f·s·r→copied), 복사 행 수 | `{"name":"messages","rows_copied":1200,"rows_total":5000,"state":"copying"}` |
| `data.readiness` | 1초 주기 | `readiness.py`: `longest_xact_ms`·`lock_waiters`(자동 변환)·`commits_per_sec`·`ready` | `{"longest_xact_ms":35,"lock_waiters":0,"commits_per_sec":41.5,"ready":true}` |
| `data.progress`·`data.verify` | 덤프·검증 단계 | 행 수·체크섬 | `[없음]` |

### 트래픽 (상헌님) — `phase: traffic`

플랫폼 Activity(`shift_traffic`)는 스캐폴드다. 필드는 웹 `[가정]` 그대로이며 #61–#63 필드가 정해지면 맞춘다. PoC 중계는 HAProxy다(Envoy 코드 없음).

| 보낼 이벤트 | 어느 Activity에서 | 값 | 예시 |
| --- | --- | --- | --- |
| `traffic.prepare` | `traffic_prepare`(사전 확인 끝) | 인계 상태 READY·`external_endpoint`(`get_migration_handoff`의 `TRAFFIC_MIGRATION` 결과), 중계·정책 | `{"state":"done","checks":[{"id":"handoff","state":"done"}],"entry_host":"app.example.com"}` |
| `traffic.snapshot` | 가중치 변경·관찰 주기마다 | 가중치·게이트·잔여 연결 | `{"phase":"observing","weights":{"origin":50,"target":50},"step":50,"gate":{…},"residual":{…}}` |
| `traffic.ready` | 전환 조건 확인 | 복제 지연·가장 긴 트랜잭션·초당 커밋 | `{"state":"done","lag_ms":0,"longest_tx_ms":12,"commits_per_sec":40}` |

### 전환·인계 (상헌님·데이터 담당) — `phase: verification`

| 보낼 이벤트 | 어느 Activity에서 | 값 | 예시 |
| --- | --- | --- | --- |
| `approval.required` (`gate: "cutover"`) | DB 전환 승인 대기 직전 | 제외 항목·롤백 정책 | `[없음]` |
| `cutover.step` | 전환 단계마다 | `n`(1–6), `running/done/verify`. PoC fast 순서로 보낸다(§3) | `{"n":1,"state":"done"}` |
| `cutover.result` | 측정 끝 | `delay`=쓰기 중단 ms, `checksum`=검증, 단계 시간 `step.<key>` | `{"id":"delay","value":"412ms"}` |
| `cutover.handover` | 인계 단계 | `dns`·`origin`·`access` | `[없음]` |
| `error` | 실패 | `{code, message:"", phase, retryable}` | `{"code":"SWITCHOVER_FAILED","message":"","phase":"verification","retryable":false}` |

## 3. PoC 브리지 (데이터·트래픽·전환 플랫폼 구현 전)

`cmp_worker.poc_bridge`가 Finals 저장소 `poc/demo` 패널 값을 위 이벤트로 바꿔 `migration_events`에 넣는다(업무 DB 접근 필요).

```bash
uv run python -m cmp_worker.poc_bridge watch --migration-id <id> \
  --panel http://127.0.0.1:8090 --results-dir ../Softbank-Hackathon-2026-Finals/poc/cdc/results
```

| PoC 값 | 이벤트 |
| --- | --- |
| 패널 `/api/state`(1초 sampler) `replication.phase` | `data.phase` (`copying→copy`, `catching_up→catchup`; `/api/metrics`의 한국어 표기도 인식) |
| `replication.lag`(bytes) | `data.lsn.lag_bytes` (`source_lsn`은 PoC가 샘플링하지 않아 `""`) |
| `replication.tables[]`(`state`=srsubstate, `copying_rows`) | `data.table` (`rows_total`은 PoC에 없어 0) |
| `readiness`(= `readiness.py` 기준) | `data.readiness` |
| `results/switchover-*.json`(성공 시에만 기록) `steps_ms`, `write_pause_ms`, `verify_ok` | PoC fast 순서 그대로: `step.prepare_reverse`·`step.seq_prebump`(시퀀스 사전 증가)·`step.sync_window_on`·`step.wait_ready` → `cutover.step` 1(`pause_wait_inflight`) → 2(`marker_wait`·`snapshot_seq_check`) → 3·4·6(`switch_subscriptions`: 구독 끄기/켜기 병렬) → 5(`retarget_resume`) → `step.drop_forward` → 6 `verify` → `delay`·`checksum` |
| 전환 실패(종료 코드, stderr) | `error`(`SWITCHOVER_FAILED`) + 마스킹한 stderr 끝 2,000자를 운영 로그로: `… 2> err.txt \|\| uv run python -m cmp_worker.poc_bridge switchover-failed --migration-id <id> --rc $? < err.txt` |

패널의 한국어 `stage`·`reasons` 문구는 보내지 않는다.

## 4. 읽기

- SSE `GET /migrations/{id}/events`: 연결 직후 `snapshot`(새 연결 `reset: true` + 지금까지 이벤트, 재접속 `Last-Event-ID` → `reset: false`), 이후 `id: <seq>` 이벤트, 15초 무출력 시 `: heartbeat`, done/failed/cancelled에서 닫힘. 전환 기간에는 기존 `event: status` 블록도 보낸다(`?legacy=false`로 끔).
- JSON `GET /migrations/{id}/events?after=<seq>&limit=500` → `{migration_id, events[], next_after}` (봉투에 `migration_id` 포함). 에이전트·최종 보고서용.
- 운영 로그 `GET /migrations/{id}/logs?after=<id>` → `{migration_id, logs[{id, at, level, source, activity, attempt, code, fields}], next_after}`.

## 5. 아직 없음

| 파트 | 없는 것 |
| --- | --- |
| 분석 | 스캔 안의 항목 단위 진행·heartbeat(이벤트는 스캔 Activity 끝마다). `node`·`nat_gateway`·`ebs_gb`·`s3_bucket` 수집(스캐너에 없음). `analysis.finding`은 쓰지 않는다(#226: `analysis.scan`으로 대신함) |
| 샌드박스 | `sbx_apply`가 버리는 `apply_network` 반환값(namespace·TTL·egress) 살리기, `BuildResult`를 `sbx_build`에 연결, `sandbox.tests` 출처별 개수와 `checks`(`calls_in_mapping` 등) 값을 만드는 코드, `run` 번호 규칙(웹 `[가정]`) |
| 계획(매핑) | `MappingRow`에 대상 서비스(`after`)와 노드 분류(`kind`) 값 없음 |
| 계획 게시·승인·구축(민서) | `plan.preview`(Preview `changes[]`→`resources`, `{resource_type, action, name, quantity, size}` → `{id, group, name, detail:"", size, change}`; 원본·대상 환경·보안 규칙·env 재매핑 값은 Preview에 없음), `approval.required`, ON_PREM(Agent 원격 실행) 구축 이벤트, `retest` 검사, 웹 `SseProvider`가 `event: status` 블록을 건너뛰게 하기 |
| 데이터 | 플랫폼 `copy_data` 구현 전체, `data.progress`(총 행 수·덤프 %), `data.verify`(행 수·체크섬), PoC `source_lsn`/`target_lsn` 샘플링(패널 `NODE_SQL`에 `pg_current_wal_lsn()` 추가 필요), 테이블 총 행 수 |
| 트래픽 | 플랫폼 `shift_traffic` 구현 전체, `traffic.prepare`·`traffic.snapshot` 필드 확정(#61–#63), 중계 방식(PoC HAProxy, 웹은 Envoy 가정). **롤백 기준 결정 필요: #61의 "직전 안전 값으로 되돌림" vs #63의 "원본 100%로 되돌림" — 상헌님 결정** |
| 전환 | 플랫폼 전환 Activity, 두 번째 승인(`approval.required gate: cutover`), 웹 6단계 모델과 PoC fast 순서 차이(3·4·6 동시) — 웹 표시 조정(민서), `cutover.result`의 `fail`·`loss` 측정(PoC `fault`의 `FAULT_RESULT`로 채울 수 있음) |
| 인계 | `cutover.handover`(`dns`·`origin`·`access`)를 만드는 코드 없음 |
