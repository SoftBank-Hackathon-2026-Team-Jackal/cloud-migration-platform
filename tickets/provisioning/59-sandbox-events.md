# Ticket 59 — 실제 모드 샌드박스 이벤트 발행 (build.*·sandbox.*)

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰 대기) — [Issue #230](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/230). 2026-11-06 조율 세션 지시로 착수. 담당 민서.

**샌드박스 범위 대행**: 샌드박스 루프는 박승철님 범위다. 본선(11/7~8) 일정 때문에 Provisioning이 대행한다. 루프 동작(단계 순서·분기·재시도·결과)은 바꾸지 않고 이벤트 발행만 더했다.

## 배경
웹 리허설 화면은 `build.*`·`sandbox.*` 이벤트로 그려지고 `sandbox.done{status:"PASSED"}`가 와야 "다음"이 켜진다. 실제(SSE) 모드에서 `SandboxVerificationWorkflow._run_integrated`는 이 이벤트를 내지 않았다.

## 관련 요구사항
PROV-021(비밀값 비노출). 계약: `packages/contracts/src/cmp_contracts/events.py`(웹 `events.ts`와 같은 모델), [이벤트 발행 가이드](../../docs/engineering/events-guide.md), [migration-events.md](../../specs/provisioning/migration-events.md). 웹 목업 순서: `cloud-migration-web` `src/lib/migration/script.ts`(`buildTimeline`·`reviewTimeline`).

## 선행 작업
24(이벤트 저장·발행 함수), 14B(통합 샌드박스 루프), 21(파이프라인 연결).

## 작업 범위
- 포함: `sbx_*` Activity 안 단계 이벤트, Workflow 수준 이벤트용 `sbx_emit` Activity(샌드박스 큐), Workflow의 events 문맥 전달(패치 `sandbox-events-v1`), `emit_event(_async)`의 `across_retries` 선택 인자
- 제외: 루프 동작 변경, 분석 이벤트(58), 데모 에지(57), `activities/analysis.py`·`analysis_pipeline.py`·`start_request.py`·`infra/demo/*`

## 착수 결정 (2026-11-06, Issue #230 표 + 구현 중 결정)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| E1 | 담당 | 샌드박스 범위지만 Provisioning이 대행한다. 루프 동작은 바꾸지 않고 발행만 더한다 | 본선 일정 |
| E2 | 발행 위치 | 단계 이벤트는 해당 `sbx_*` Activity 안에서, Workflow 수준 이벤트(`sandbox.attempt`·`sandbox.done`·`build.review`)는 새 `sbx_emit` Activity로 낸다. Workflow에 I/O 없음 | 결정성 |
| E3 | 중복 | 키 `sbx:{run}:{attempt}:{step}:{state}`. 이 키는 Activity 재시도(attempt 2…)에도 한 건만 남도록 `across_retries=True`로 낸다(멱등 해시에서 Activity attempt를 뺀다) | 기존 해시는 Activity attempt를 포함해 재시도가 새 이벤트였다 |
| E4 | 비밀값 | data에 값·코드·이름만. 로그·DB 비밀번호·레지스트리 토큰·`.env` 값은 넣지 않는다. `build.fix` 줄은 `mask_text`로 가리고 `.env` 계열 파일은 키 이름만 보인다 | PROV-021 |
| E5 | migration_id | 샌드박스 입력의 `migration_id`는 부모 `MigrationWorkflow` ID다(부모 ID → 분석 입력 → 보고서 → `assemble_sandbox_input`). 파이프라인 테스트가 입력·events 문맥·`sbx_emit` 모두 `migration-w`인지 고정한다 | 웹 스트림 키 |
| E6 | run·attempt | `run`은 `sbx_apply` 횟수(화면 트랙, 1~6). `attempt`는 수정 시도(1~3). INFRA 재시도는 run만 늘린다. 재빌드 이벤트는 다음 run 번호를 쓴다 | 웹 `sandboxPlans`(infra 시나리오) |
| E7 | render 단계 | 매니페스트 렌더는 `sbx_prepare`에서 끝나므로 `sbx_apply` 시작에서 render running→done을 낸다(모든 run에 render가 보이게). RENDER 분류는 `sandbox.attempt.failure_class`로 보인다 | 웹 트랙은 run마다 render부터 그린다 |
| E8 | test·judge | `sbx_verify`가 `sandbox.tests`(고정 묶음 출처별 개수), test running→done/failed, judge running→done/failed(판정 실패면 failed)를 낸다. 판정은 `sbx_verify` 안에서 이뤄지므로 `sbx_classify`가 아니라 여기서 낸다. 통과 경로는 `sbx_classify`를 거치지 않는다 | 웹 목업: 테스트 실패는 judge에서 failed |
| E9 | sandbox.attempt | Workflow가 판정 직후(통과·분류 뒤) 낸다. `sbx_fix`가 고친 것이 있으면 같은 run을 `fix_ids`와 함께 한 번 더 낸다(웹 reducer가 run으로 덮어쓴다). `checks`는 `sbx_verify`가 돌려준다 | `fix_ids`는 수정 뒤에야 안다 |
| E10 | sandbox.done | 정리 전 `PENDING`(running), 정리 뒤 `CLEANED`(PASSED면 done, 아니면 failed). Sandbox가 만들어지기 전 끝난 실행(첫 빌드 실패)은 정리할 것이 없어 `CLEANED` 한 건. 통과 뒤 게시 오류·재승인 차단으로 `NEEDS_REVIEW`가 되면 한 건 더 낸다 | 웹 목업 순서. 재승인 대기 중에는 이미 PASSED라 "다음"이 켜진다(데모 경로에는 재승인 없음) |
| E11 | build.review | 3회 실패로 `NEEDS_REVIEW`가 될 때만 `{reason:"max_attempts", attempts:3}`(waiting), `sandbox.done` 뒤 | 계약 reason 값이 `max_attempts` 하나 |
| E12 | 빌드 실패 | `build.check`(failed)·`build.result`(FAILED, 계약 밖 코드는 `BUILD_FAILED`) 뒤 Workflow가 `sandbox.attempt{failure_class:"BUILD"}`를 낸다 | 웹 `reviewTimeline` |
| E13 | 발행 실패 | `sbx_emit` 실패(Worker 없음·저장 실패)는 잡아 로그만 남기고 루프는 그대로 간다 | 이벤트는 관찰(E9 규칙) |
| E14 | 재생 호환 | `workflow.patched("sandbox-events-v1")`. 이 티켓 전 이력은 events 문맥·`sbx_emit` 없이 재생된다(픽스처 `tests/fixtures/sandbox/sandbox-before-59-code-fix.json`) | 실행 중 Workflow 보호 |

## 단계 → 이벤트

| 단계 | 이벤트 | 위치 |
| --- | --- | --- |
| `sbx_build` | `build.check{id:"build", state running→done/failed, attempt}` → `build.result{attempt, max_attempts:3, status, digest, image_uri, reused, error_code}` | Activity |
| `sbx_prepare`(첫 번째) | `sandbox.env{namespace, expires_minutes, egress_allowed}` | Activity |
| `sbx_apply` | `sandbox.step` render → apply → ready (running·done/failed) | Activity |
| `sbx_ai_tests`·`sbx_verify` | `sandbox.tests{total, catalog, original, generated, read_only}` + `sandbox.step` test·judge | `sbx_verify` |
| 판정 뒤 | `sandbox.attempt{run, attempt, max_attempts, verdict, failure_class, checks, fix_ids}` | Workflow → `sbx_emit` |
| `sbx_fix` | 고친 파일마다(요구사항 수정은 한 건) `build.fix{id:"fix-{attempt}-{n}", check, lines[], why, evidence, file}` | Activity |
| 3회 실패 | `build.review{reason:"max_attempts", attempts}` | Workflow → `sbx_emit` |
| 끝 | `sandbox.done{status, cleanup_status, digest}` PENDING → CLEANED | Workflow → `sbx_emit` |

모든 이벤트는 `phase: build`. 상태는 `running`(재시도할 실패 포함), `sandbox.done` 최종은 `done`/`failed`, `build.review`는 `waiting`.

## 검증 기록
- `tests/sandbox/test_sandbox_events.py`(가짜 서비스·빌더, InMemoryEmitter): 각 이벤트 계약 모델 검증, 빌드 성공·실패(계약 밖 코드 → `BUILD_FAILED`), env 1회, apply 단계 순서·실패 단계, verify tests·test·judge·checks, apply 실패 뒤 테스트 단계 없음, `build.fix`(코드·요구사항), `.env` 키만, 재시도 중복 없음(`ActivityEnvironment` attempt 2), 비밀값 비노출
- `tests/sandbox/test_sandbox_events_workflow.py`: 통과 1회, CODE 실패 → 수정 → 재빌드 → 통과, INFRA 재시도(run만 증가), 3회 실패 → done failed → `build.review`, RENDER 중단, 첫 빌드 실패, `sbx_emit` 실패에도 결과 동일, 이 티켓 전 이력 재생, 새 이력 재생
- `tests/analysis/test_pipeline_wiring.py`: 입력·events 문맥·`sbx_emit`의 `migration_id`가 부모 ID(`migration-w`)
- `make check`, alembic check, `make openapi`(변경 없음)

## 남은 일
- 실제 샌드박스 클러스터에서 한 번 돌려 화면 확인(서버 반영 후)
- `build.fix.id`는 `fix-{attempt}-{n}`이라 웹 AI 팁(`rehearsal.aiTip.<id>`) 문구 키와 맞지 않는다. 웹이 모르는 id의 팁을 숨기는지 웹 담당 확인
