# Ticket 57 — 데모 에지 /migrations·/provisioning SSE 라우트와 리허설 기본 테스트 경로

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·데모 서버 반영 승인 대기) — [Issue #228](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/228). 2026-11-06 조율 세션 지시로 착수. 담당 민서.

**범위 대행**: 웹 실제 모드의 이벤트 경로·리허설 테스트 모양은 분석·샌드박스·트래픽 담당 범위지만, 본선(11/7~8) 일정 때문에 Provisioning(김민서)이 대행한다. 분석·샌드박스 Activity와 Workflow 코드는 바꾸지 않는다(같은 시기 58·59가 발행을 맡는다).

## 배경
웹 실제(SSE) 모드(#224, 병합)는 브라우저가 공개 주소의 `POST /migrations`, `GET /migrations/{id}/events`(SSE)와 Plan 승인 `POST /provisioning/plans/{plan_id}/revisions/{revision}/approvals`를 부른다. 데모 에지(Caddy, `infra/demo/agent-routes.caddy`)는 `/agents/*`·`/relays/*`만 API로 넘겨, 나머지는 웹이 404로 답했다. 또 웹 시작(`start_request.start_input`)은 `"sandbox": None`이라 리허설(Sandbox 표준 테스트)이 헬스 테스트만 돌았다.

## 관련 요구사항
PROV-021(연결·권한 경계). 21 D3(API 루프백 바인딩, 공개 경로는 에지만), 44 S10(시작 입력), 45(데모 실행 설정), 분석·검증 27(표준 테스트 쓰기 모양, `REPEAT_WRITE`).

## 선행 작업
21(데모 에지), 44(웹 시작), 45(데모 실행 설정). 웹 실제 모드 빌드 #224.

## 작업 범위
- 포함
  - `agent-routes.caddy`의 `@backend`에 `/migrations` `/migrations/*` `/provisioning/*` 추가, `reverse_proxy`에 `flush_interval -1`
  - API 설정 `WEB_START_SANDBOX_SETTINGS`(SandboxSettings JSON 객체) → 웹 실제 시작의 `sandbox`
  - compose api 환경 변수, `runtime.env.example`의 데모 앱 줄, `runtime-check.sh` 선택 이름, API `runtime_report`의 `web_start_sandbox=`
  - README·shared-contracts(44 시작 입력) 갱신
- 제외
  - 분석 Activity(`activities/analysis.py`)·Sandbox Workflow·Activity, 이벤트 발행(58·59)
  - `/migrations` 인증(웹 로그인과 API 확인) — 후속
  - 데모 서버 배포·확인(오케스트레이터가 사용자 승인 후)

## 착수 결정 (2026-11-06, Issue #228 표 + 진행 중 결정)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| E1 | 담당 | 분석·샌드박스·트래픽 범위지만 본선 일정 때문에 Provisioning(김민서)이 대행한다 | Issue #228 |
| E2 | 공개 경로 | `/migrations`, `/migrations/*`, `/provisioning/*`만 추가한다. `/provisioning-admin/*`, `/agent-admin/*`, `/tunnel-admin/*`, `/connections`, `/env-files`, `/agent-enrollments`, `/docs`, `/openapi.json`은 계속 웹으로 간다(API에 닿지 않음) | 웹 실제 모드가 부르는 경로만 연다. Caddy `path /provisioning/*`는 `/provisioning/` 접두만 맞아 `/provisioning-admin`은 걸리지 않는다(실제 Caddy로 확인) |
| E3 | SSE 버퍼링 | `reverse_proxy { flush_interval -1 }` | 응답 조각을 바로 보낸다. Caddy는 `text/event-stream`을 기본으로도 바로 보내지만 설정으로 고정한다 |
| E4 | /migrations 인증 | 데모용으로 무인증 공개를 감수한다(흐름 점검 N11 공백). 실행 ID(`migration-<uuid4>`)를 아는 사람은 상태·이벤트·로그를 읽고 `approve`·`cancel`을 보낼 수 있다. 데모 뒤에는 경로를 빼거나 에지를 내리고, 운영 전 사용자 인증이 먼저다. README에 기록 | Issue #228. `/provisioning/*` 승인 경로는 API의 승인자 자격 증명·관리자 키 확인을 그대로 받는다 |
| E5 | 리허설 기본값 위치 | 서버 환경 변수 하나(`WEB_START_SANDBOX_SETTINGS`, SandboxSettings JSON 객체)로만 둔다. 코드에 앱 경로를 넣지 않는다. 비어 있으면 `sandbox: null`(헬스 테스트만, 기존 동작) | Issue #228. CLI `--sandbox`와 같은 값이라 runbook(분석·검증 27) 모양을 그대로 쓴다. 필드를 하나씩 환경 변수로 나누면 `write_body` 같은 객체를 다시 조립해야 한다 |
| E6 | 검증 범위 | API는 "JSON 객체인가"만 본다. 아니면 실제 시작이 503 `SANDBOX_SETTINGS_INVALID`(값을 응답에 넣지 않음). 필드는 worker가 `assemble_sandbox_input`에서 `SandboxSettings`로 검증한다 | API는 `cmp_worker`를 import하지 않는다(44 S10). 잘못된 운영 설정은 시작 전에 막고, 필드 단위 검증은 한곳에 둔다 |
| E7 | 데모 앱 값 | `runtime.env.example`에 demo-app(`main.go`) 모양: `write_path=/api/messages`, `write_body={client_msg_id: "std-{{run_id}}", author: "cmp-rehearsal", body: "value-{{run_id}}"}`, `write_status=201`, `id_from_response=id`, `value_field=body`, `read_back=REPEAT_WRITE`. compose 기본값으로는 두지 않는다 | demo-app은 `GET /api/messages/{id}`가 없고, 같은 `client_msg_id` 재전송에 200과 저장된 행을 준다(멱등 쓰기). 다른 앱을 시작해도 이 모양이 붙지 않게 운영자가 켠다. compose `${VAR:-기본값}` 안의 `{}`는 해석이 깨질 수 있다 |
| E8 | scaffold 시작 | `target` 없는 시작은 그대로(문자열·`env_file_ref`만) | 기존 계약 유지 |

## 검증 기록 (2026-11-06, 로컬)
- `tests/test_demo_runtime.py`
  - 에지 라우트 전체 줄 비교(`@backend` 경로 5개, `flush_interval -1`)
  - api 환경 `WEB_START_SANDBOX_SETTINGS: ${WEB_START_SANDBOX_SETTINGS:-}`(worker에는 없음), `runtime.env.example` 값이 `SandboxSettings`로 검증되고 demo-app 모양(201·`id`·`body`·`REPEAT_WRITE`)이며 표준 케이스 3개(헬스·왕복·재시작 후 유지)를 만든다
  - API `runtime_report`의 `web_start_sandbox=off/on/error:SANDBOX_SETTINGS_INVALID`
- `tests/test_web_start_request.py`
  - 설정 없음 → `sandbox: null`, 설정 있음 → 그 객체이고 CLI `build_start_input(sandbox_json=…)`과 같은 입력, worker `SandboxSettings`로 검증됨
  - JSON 아님·배열·문자열·null → 503 `SANDBOX_SETTINGS_INVALID`, 시작 없음
  - scaffold 시작은 설정과 무관
- 실제 Caddy(`caddy:2.10.2-alpine`, compose와 같은 digest)
  - `caddy adapt --validate`로 실제 `Caddyfile` + `agent-routes.caddy` 통과(API reverse_proxy `flush_interval: -1`, 경로 5개)
  - 같은 라우트를 HTTP로 띄우고 가짜 API·웹으로 확인: `/migrations`, `/migrations/m-1`, `/migrations/m-1/logs`, `/provisioning/plans/…/approvals`, `/agents/register`, `/relays/r/agents` → API. `/provisioning-admin/teardowns`, `/agent-admin/agents`, `/connections`, `/env-files`, `/agent-enrollments`, `/tunnel-admin/relays/r`, `/docs`, `/openapi.json`, `/migrationsX`, `/health`, `/` → 웹
  - SSE: 3초 간격 이벤트 두 개가 에지를 지나 3초 간격 그대로 도착(버퍼링 없음)
- 웹(`cloud-migration-web`)에 `/migrations`·`/provisioning` 화면 경로가 없어 가려지는 화면이 없다
- `make check`, alembic(SQLite) upgrade·check, `make openapi`

## 미검증·남은 일
- 데모 서버 반영과 공개 주소 확인(오케스트레이터, 사용자 승인 후). `/opt/cmp/.env`에 `WEB_START_SANDBOX_SETTINGS` 줄 추가는 실제 Sandbox 루프(`SANDBOX_KUBECONFIG`)가 켜질 때 의미가 있다
- `/migrations` 사용자 인증(N11)
- 리허설 쓰기 모양의 앱별 자동 선택(AI 테스트 경로, 분석·검증 담당)
