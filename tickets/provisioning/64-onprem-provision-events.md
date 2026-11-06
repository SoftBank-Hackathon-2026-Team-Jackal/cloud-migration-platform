# Ticket 64 — ON_PREM 구축 이벤트 (provision.resource·provision.check)

## 상태
DONE (단위·Activity 시험 통과, 실제 Agent 화면 확인은 데모 리허설 때). [Issue #281](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/281). 2026-11-07 조율 세션 지시. 담당 민서.

## 현상
ON_PREM 구축 Activity `execute_on_prem_plan`은 Agent 작업을 넣고 결과를 기다리기만 하고 웹 이벤트를 하나도 내지 않았다.
웹 구축 화면(`ProvisioningStage.tsx`)은 **마지막 리소스의 progress가 100이고 받은 check가 모두 done**일 때 끝난 것으로 본다. 그래서 Agent가 READY를 보내도 화면이 0%에서 멈추고, 실패해도 실패 블록이 안 그려졌다.

## 결정

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| D1 | 리소스 목록 | Agent 작업 내용(승인된 Revision에서 만든 `ProductionRolloutJob`)에서: Namespace(`cluster`), 승인된 NetworkPolicy가 있으면(`network`), DB release(`db`), 앱 release(`app`). id는 `<job_id>:<부분>` | Preview `changes`는 환경변수·정책 줄까지 있어 블록이 너무 잘다. 작업 내용이 실제로 배포되는 것과 같다 |
| D2 | 진행 | Agent는 리소스별 진행을 보내지 않는다. 작업을 넣으면 모두 `running`·0, READY면 순서대로 `done`·n/m(마지막 100) | 브리프의 "작업 단위" 규칙. 웹 완료 조건을 만족 |
| D3 | 검사 | READY면 `pods_ready`·`db_connect`·`app_health` 모두 `done`. 실패 코드가 Agent `verify()` 검사 하나에 해당하면 앞 검사는 `done`, 그 검사는 `failed` | Agent `verify()` 순서(PVC·워크로드·엔드포인트 → pg_isready → health)와 웹 검사 이름 |
| D4 | 실패 | READY가 아닌 결과(FAILED, EXPIRED→`AGENT_OFFLINE`, `UNKNOWN AGENT_RESULT_PENDING` 포함)는 모든 리소스 `failed` + `detail`=코드. 작업을 넣지도 못하면 `agent` 블록 하나 `failed` | Workflow가 READY 아니면 구축 실패로 끝나므로 화면도 멈춘 채로 두지 않는다 |
| D5 | 멱등 | 키 `onprem:<id>:<state>`·`onprem:<job_id>:check:<name>:<state>`, `across_retries=True` | 작업 id는 Revision마다 하나라 재시도·재실행에도 한 줄 |
| D6 | 안전 | Activity 안에서만 낸다(Workflow 변경 없음 → replay 영향 없음). 발행 실패는 결과를 바꾸지 않고 운영 로그 `PROVISION_EVENTS_UNAVAILABLE`. 결과 코드는 `log_result`로 운영 로그에도 남긴다 | events-guide E9 |
| D7 | 비밀값 | 리소스 이름·release·Namespace·결과 코드만. Secret 이름·키, Digest 없음 | 시험으로 확인 |

Agent 판정·적용 로직, 클라우드 경로 이벤트, 웹 코드는 바꾸지 않았다.

## 확인
- `tests/test_onprem_provision_events.py`: READY(순서·progress·웹 완료 조건), 실패(`DATABASE_NOT_READY`: 리소스 failed + 코드, `pods_ready` done·`db_connect` failed), Agent 오프라인, 작업 거절, 같은 Revision 재실행 시 중복 없음, 비밀값 없음. 모든 이벤트가 `validate_data`를 통과.
- 전체 `pytest`·`ruff check`·`ruff format --check` 통과.
