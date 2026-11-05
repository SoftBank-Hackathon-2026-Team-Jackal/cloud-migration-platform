# ENV-04 — 구축(v2)을 render_env로 전환

## 담당
민서 (Provisioning 20, [#99](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/99) 재사용)

## 상태
IN_PROGRESS — GitHub Issue [#99](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/99) 재사용. 착수 결정은 아래 표(2026-11-06). ENV-05(분석 EnvPlan 출력)가 아직 없어, 이번 범위는 Planning 입력으로 받은 EnvPlan을 구축에서 렌더하는 데까지다. 조정 세션 검토 전까지 IN_PROGRESS로 둔다.

## 목적
운영 구축이 사람이 미리 만든 ConfigMap·Secret 없이, 승인된 `env_file_ref`와 EnvPlan으로 대상에 앱 env Secret을 만들고 참조를 연결한다.

## 관련 요구사항
ENV-007, ENV-008. PROV-004, PROV-010, PROV-012, PROV-021.

## 선행 작업
ENV-02, ENV-03, ENV-05(EnvPlan). Provisioning 09·11·12·18·19·22 (DONE).

## 현재
`plan_publish`가 `environment.env_file_ref`를 앱 Values의 `config_ref`로 넘길 뿐, 값을 대상에 만들지 않는다. 대상에 없으면 `CONFIG_MAP_MISSING`·`USER_SECRET_MISSING`(AWS/GCP), `SECRET_REFERENCE_MISSING`(ON_PREM)으로 실패한다.

## 범위
- 포함
  - Plan 게시: EnvPlan을 Plan 입력 Digest에 넣고, Preview에는 키 이름·처리 결과만 표시한다. `decisions`가 있으면 결정 필요 1번 정책대로 처리한다.
  - AWS/GCP 배포 Activity: `render_env(EnvPlan, env_file_ref, 대상 DB 출력)` 결과로 `<앱 release>-env` Secret을 만들거나 갱신하고(플랫폼 관리 라벨, 관리 밖 같은 이름은 `SECRET_NAME_CONFLICT`), 앱 Values env를 `secretKeyRef`로 연결한다.
  - DB 연결 Secret `<DB release>-conn`(키 `host`·`port`·`name`·`username`, 데이터는 `connection_secret_data(outputs)`)을 만들고 `DbOutputRefs.from_outputs(...)`로 `render_env`에 넘긴다(ENV-03 D3).
  - ON_PREM: Agent가 클러스터 안에서 같은 결과를 적용한다. 값은 작업 본문에 넣지 않고 Agent 인증 API로 작업별로 받는다.
  - 승인 뒤 `env_file_ref` 버전이 바뀌면 실행하지 않는다(Digest 불일치).
  - 티켓 20 문서를 이 티켓으로 연결, SDD 갱신
- 제외: 분석 EnvPlan 생성(ENV-05), 샌드박스(ENV-06)

## 수용 기준
- [x] 사람이 미리 만든 ConfigMap·Secret 없이 09·11·12 구축이 성공한다. (가짜 클러스터·Agent 기준. 실제 클러스터는 미검증)
- [x] 샌드박스와 같은 `env_file_ref`·EnvPlan을 쓰고 DB 출력만 다르다. (같은 `render_env`, 대상 DB 출력은 `DbOutputRefs.from_outputs`. ENV-06 연결은 샌드박스 담당)
- [x] 원문 값이 Values·Plan·Preview·작업 본문·결과·이벤트·로그·Temporal 이력에 없다. (문자열 검사: 플랫폼이 쓴 모든 파일·SQLite 업무 DB·Activity 결과·trace·로그. 값은 Agent 전용 API 응답과 클러스터 적용 직전 메모리에만 있다)
- [x] 관리 밖 같은 이름 Secret은 덮어쓰지 않는다.
- [x] `env_file_ref`가 없는 기존 Plan은 그대로 동작한다. (EnvPlan이 없으면 기존 경로. 기존 테스트 무변경 통과, 작업 본문·등록 바이트 동일)

## Test Seam
09·11의 가짜 ClusterSession, 12의 가짜 클러스터, InMemory `.env` 저장소. 비노출은 19와 같은 문자열 검사.

## 결정 필요
- ~~1. `UNREPRESENTABLE`·기본값 없는 `MISSING`을 승인 전에 막을지~~ → 승인 전에 막는다(ENV-03 D1). 구축 쪽 처리 방식은 아래 E2.

## 착수 결정 (2026-11-06)

| # | 항목 | 결정 |
| --- | --- | --- |
| E1 | EnvPlan 출처 | ENV-05 전까지 Planning 입력 등록(티켓 18 `PlanningInputRegistration`)의 선택 필드 `env_plan: EnvPlan`으로 받는다(운영자 또는 나중에 분석이 넣는다). 키 이름·처리 결과만 있고 값은 없다. 등록 시 `env_plan.env_file_ref == planning_request.environment.env_file_ref`가 아니면 `ENV_PLAN_REF_MISMATCH`. 게시할 때 승인 대상 SPEC 산출물(`spec_document.env_plan`)에 넣어 manifest Digest로 고정한다. 실행 쪽(배포 Activity, Agent 비밀 API)은 이 산출물에서만 읽는다. 분석 코드는 건드리지 않았다. |
| E2 | Digest | ON_PREM: `input_digest`에 `env_plan`을 넣는다. AWS/GCP: WORKLOADS Plan Digest에만 넣는다(인프라 Plan은 다시 만들지 않는다, 22 W3과 같은 방식). EnvPlan이 없으면 Digest·저장 바이트가 이전과 같다(`exclude_if`). |
| E3 | Preview와 결정 | Plan 게시 때 `preview_env`(키 이름만 읽음)로 `render_env`와 같은 `env`·`decisions`를 계산한다. `decisions`(`ENV_UNREPRESENTABLE`·`ENV_MISSING`·`ENV_VALUE_MISSING`·`ENV_DB_OUTPUT_MISSING`)는 `pending_decisions`(코드, 경로 `environment.env_plan.<KEY>`)로 넣고 게시한다. 07이 `PENDING_DECISION`으로 승인을 거절한다(18 P5의 `CLEANUP_POLICY_REQUIRED`와 같은 규칙). 사람이 Preview에서 어떤 키가 막혔는지 볼 수 있게 `errors`(게시 안 함) 대신 이쪽을 골랐다. Preview `applied_defaults`에는 `<app>-env` Secret의 PASS 키 이름, REPLACED 바인딩(`DB_HOST→host`), `<db>-conn` 생성, EXCLUDED 키 이름만 넣는다. 저장소 미설정은 `ENV_FILE_STORE_UNCONFIGURED`, 저장소 오류는 `ENV_FILE_UNAVAILABLE`(+`ENV_FILE_EXPIRED` 등)로 게시하지 않는다. |
| E4 | Values 연결 | EnvPlan이 있으면 요구사항의 `env_bindings` 대신 EnvPlan으로 앱 env를 만든다. PASS와 REPLACED `host·port·name·username`은 Secret 참조(`<app>-env`/KEY, `<db>-conn`/키), REPLACED `password_secret_ref`·`connection_url_secret_ref`는 기존 DB 출력 바인딩(-auth, 19의 -url 단계 유지). v2(ON_PREM)는 `SECRET`, v1(09/11)은 `USER_SECRET`/`DATABASE`로 같은 `secretKeyRef`가 된다. 요구사항 `env_bindings`가 EnvPlan과 다르면(같은 키가 REPLACED·같은 binding이 아니면) `PLANNING_INPUT_INVALID`/`ENV_PLAN_BINDING_MISMATCH`. |
| E5 | AWS/GCP 실행 | 배포 Activity가 SPEC 산출물의 EnvPlan과 Values `config_ref`(같은 참조여야 함)로 `render_env(store, plan, ref, DbOutputRefs.from_outputs(대상 DB 출력), app_release)`를 부른다. 결과 env가 승인된 Values의 Secret 참조와 다르면 `ENV_BINDING_MISMATCH`. `<app>-env`·(참조될 때) `<db>-conn`을 모두 충돌 검사한 뒤 `apply_secret`(stdin, 플랫폼 관리 라벨)으로 만든다. 관리 밖 같은 이름은 `SECRET_NAME_CONFLICT`, Helm 전에 멈춘다. 그다음 기존 `USER_SECRET`·`CONFIG_MAP` 존재 확인을 그대로 한다. 저장소는 워커가 API 설정(`ENV_FILE_STORE_DIR`·`ENV_FILE_ENCRYPTION_KEY`)으로 연다(`cmp_api.env_store`). 결과에는 Secret 이름만(`env_secrets`). |
| E6 | ON_PREM 값 전달 | 작업 본문에는 이름·키만(`application.env_secrets: [{name, keys}]`, 비어 있으면 직렬화에서 빠짐). 이 Secret들은 사용자 사전 생성 목록(`secret_refs`)에서 뺀다. Agent는 Helm 전에 `GET /agents/{agent_id}/jobs/{job_id}/env-secrets`(Agent 인증, 자기 RUNNING 작업만, 12의 산출물 바이트 API와 같은 방식)로 `[{name, data}]`를 받는다. 플랫폼은 요청마다 승인된 Revision을 다시 확인하고(`check_dispatchable`) SPEC 산출물의 EnvPlan으로 `render_env`를 부른다(작업 본문을 믿지 않는다). 응답은 `Cache-Control: no-store`. Agent는 이름·키를 확인한 뒤 플랫폼 관리 라벨로 클러스터에 적용한다. 관리 밖 같은 이름은 `SECRET_NAME_CONFLICT`, 받지 못하면 `ENV_SECRETS_UNAVAILABLE`(UNKNOWN), 이름·키가 다르면 `ENV_SECRETS_INVALID`. **Agent 코드가 바뀌므로 새 Agent 릴리스가 필요하다(사용자 승인 뒤 태그).** 이전 Agent는 새 필드가 있는 작업을 `UNSUPPORTED_JOB`으로 거절한다(안전한 실패). |
| E7 | 승인 뒤 변경 | 승인된 Plan은 SPEC 산출물의 `env_file_ref`(id·version·sha256)만 읽는다. 저장된 바이트가 그 sha256과 다르면 `ENV_FILE_DIGEST_MISMATCH`, 만료면 `ENV_FILE_EXPIRED`로 Helm 전에 멈춘다. 새 버전을 다시 등록하면 입력 Digest가 바뀌어 새 Revision이 되고 이전 승인은 `STALE_REVISION`이다. |
| E8 | GKE `apply_secret` 형태 | `GkeKubectlSession.apply_secret`는 `(ns, name, type, data)`만 받아 공통 3인자 호출(19 URL Secret, 이번 env Secret)과 맞지 않았다. 두 형태를 모두 받도록 고쳤다(3인자는 Opaque). |

## 구현 위치
- 공통: `cmp_providers.env_file` — `preview_env`(Plan 시점, 키 이름만), `database_outputs`(승인된 Values에서 운영 DB 출력), `deployment_secrets`(`-env` + 참조될 때 `-conn`)
- 계약: `PlanningInputRegistration.env_plan`, `RolloutApplication.env_secrets`/`RolloutEnvSecret`
- Planning: `cmp_api.plan_inputs`(등록 검사·Digest), `cmp_worker.plan_publish`(`_env`, SPEC 산출물, Preview), `cmp_providers.onprem_preview`(`platform_secrets`)
- AWS/GCP: `cmp_worker.activities.k8s_workloads._apply_env`, 워커 연결 `aws_workloads_cli`·`gcp_workloads_cli`·`activities/plan_publish`, `cmp_api.env_store`
- ON_PREM: `cmp_api.onprem_rollout`(`build_rollout_job`, `job_env_secrets`), `GET /agents/{agent_id}/jobs/{job_id}/env-secrets`, Agent `cmp_providers.onprem_agent`·`remote_agent.env_secrets`
- 테스트: `tests/test_env_provisioning.py`, `tests/test_env_file.py`(ENV-04 부분)

## 검증 (2026-11-06)
- `tests/test_env_provisioning.py` 26개: ON_PREM 게시(Secret 참조 연결, Preview 키 이름만, `read_values` 0회), 결정 → `pending_decisions`·승인 거절, PASS 값 없음 → `ENV_VALUE_MISSING`, 저장소 미설정·만료, 참조 불일치, 요구사항 바인딩 충돌, Digest 범위, 작업 본문 `env_secrets`·Agent API(인증·소유·RUNNING·만료 코드), 새 버전 등록 뒤 `STALE_REVISION`, EnvPlan 없는 작업 바이트 동일, Agent 적용 순서·관리 Secret 갱신·사용자 Secret 충돌·가져오기 실패, AWS/GCP 인프라→워크로드 Plan→09/11 배포(미리 만든 ConfigMap·Secret 없음, `-env`·`-conn` 데이터, `USER_SECRET` 참조), 충돌·만료·저장소 미설정·바이트 변조 시 Helm 전 중단, 승인된 버전만 사용. 모든 경우 값 문자열 검사.
- `tests/test_env_file.py` +6: `preview_env`와 `render_env`의 `env`·`decisions` 일치(값 읽지 않음), `deployment_secrets`.
- `make check`, `make openapi`(Agent env-secrets 경로, 등록 스키마의 `EnvPlan` 추가 확인). DB 스키마 변경 없음(`alembic check` 해당 없음). Temporal Workflow 변경 없음.
- 미검증: 실제 EKS·GKE·K3s 클러스터, 실제 Agent 이미지(새 릴리스 필요), 운영 API·워커에 `.env` 저장소 설정 배포.
