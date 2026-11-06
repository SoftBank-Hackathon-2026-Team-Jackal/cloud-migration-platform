# Ticket 58 — 실제 모드 분석 이벤트 발행 (`analysis.finding`·`analysis.done`)

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰 대기) — [Issue #229](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/229). 2026-11-06 조율 세션 지시로 착수. **분석 범위 대행**: 원래 분석 담당(박승철님) 범위지만 본선(11/7~8) 일정 때문에 Provisioning(김민서)이 대행한다. 스캔 결과 계산은 바꾸지 않고 발행만 더한다.

## 배경
웹 분석 화면(`cloud-migration-web` `AnalysisStage.tsx`)은 SSE `GET /migrations/{id}/events`의 `analysis.finding`으로 열(code·infrastructure)을 채운다. 각 열의 막대는 그 흐름 마지막 finding의 `progress`를 따르고, **두 흐름이 모두 100이 되어야 "다음"이 켜진다**. `analysis.done`은 분석 완료 표시(음성 안내·뒤로가기 재생)다. 지금까지 worker는 두 이벤트를 내지 않아 실제 모드 화면이 비고 멈췄다.

## 관련 요구사항
PROV-021(비밀값·문구 없는 이벤트). 계약: `cmp_contracts.events`(`AnalysisFindingData`·`AnalysisDoneData`), [migration-events.md](../../specs/provisioning/migration-events.md) E7(멱등), [events-guide.md](../../docs/engineering/events-guide.md) §2 분석.

## 선행 작업
24(이벤트 저장·`emit_event`), 19(실제 분석 Activity). 없음(블로커).

## 작업 범위
- 포함
  - `cmp_worker.analysis_events`: 스캔 결과 → `(key, 개수, 값)` 항목, 발행(`emit_findings`·`emit_done`)
  - `AnalysisActivities`(실제 분석) 6개 Activity 끝에 발행: `fetch_source`·`scan_manifests`·`scan_code`·`scan_aws_infra`·`build_requirements`, `check_evidence` 끝에 종료 표시 + `analysis.done`
- 제외
  - 스캐너(`packages/scanners`) 계산 로직, Workflow 코드(`workflows/analysis.py`) 변경
  - 스캐폴드 Activity(문자열 입력 분석)는 발행하지 않는다
  - 웹 저장소 변경, 계약(`events.py`) 변경(`analysis.scan` 미도입)

## 착수 결정 (2026-11-06, Issue #229 표 + 구현 중 결정)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| A1 | 담당 | 분석 범위 대행(Provisioning). 계산은 그대로, 발행만 | Issue #229 |
| A2 | 이벤트 종류 | 계약에 있는 `analysis.finding`만 쓴다. 웹의 새 `analysis.scan`(스캔 묶음)은 백엔드 계약(`DATA_MODELS`)에 없어 도입하지 않는다 | 계약 변경은 별도 결정. 웹은 scan이 없으면 finding 방식으로 그린다 |
| A3 | 발행 위치 | 스캔 Activity가 **성공한 뒤** Activity 안에서 낸다(`AnalysisActivities` 래퍼). Workflow는 바꾸지 않는다 | 결정성. 실패한 시도는 아무것도 내지 않는다 |
| A4 | key·stream·via | 아래 "key별 발행 위치" 표. 값이 없는 key는 내지 않는다. `node`·`nat_gateway`·`ebs_gb`는 읽기 전용 AWS 스캔(`AwsInfraScan`)에 값이 없어 **내지 않는다** | Issue 범위 "값이 없는 key는 내지 않는다" |
| A5 | `scan_network` | 실제 Workflow에는 `scan_network` Activity가 없다(네트워크는 `build_requirements` 안에서 계산). `vpc`·`subnet`·`security_group`은 값의 출처인 `scan_aws_infra`에서 낸다 | Workflow 변경 없이 같은 값 |
| A6 | 문구 | `message`는 `""`, `detail`은 값만(개수, 필요하면 ` · ` 뒤에 비밀 아닌 식별자 하나: 커밋 12자, 클러스터 이름·버전, VPC CIDR, 드라이버 모듈, 프로브 경로, 리소스 종류별 수) | events-guide "문구는 보내지 않는다", 계약 docstring(PROV-021). 웹이 key로 문구를 고른다 |
| A7 | progress | 흐름별 구간: code `fetch_source` 10–20 → `scan_manifests` 30–50 → `scan_code` 55–75 → `build_requirements` 85–90, infrastructure `scan_aws_infra` 15–90. 구간 안에서 항목마다 오른다. 100은 `check_evidence`의 종료 표시(`code-done`·`infrastructure-done`, 웹이 열에서 빼는 key)만 쓴다 | 웹 목업 순서. "다음"은 두 흐름 100에서 켜지므로 분석 보고서가 나오기 전에는 켜지지 않는다. `scan_code`·`scan_manifests`·`scan_aws_infra`는 병렬이라 code 흐름 막대가 잠깐 뒤로 갈 수 있다(종료 표시가 100을 보장) |
| A8 | `analysis.done` | `check_evidence` 성공 직후 종료 표시 2건 → `analysis.done`(data `{}`, status `done`, progress 100) 1건. 그 앞 단계가 실패(`ANALYSIS_BLOCKED`·`MAPPING_NEEDS_REVIEW`·Activity 실패)하면 내지 않는다 | 분석 보고서가 만들어진 시점이 "분석 끝". 사람 검토 게이트(`confirm_requirements`)는 그 뒤에 온다 |
| A9 | 멱등 | `idempotency_key = ana:{activity}:{key}`(done은 `ana:check_evidence:done`). 같은 시도 안 반복은 한 번만 남는다. Temporal 재시도(새 attempt)는 E7대로 새 행이 되지만 `data.id`가 같아 웹이 덮어쓴다(`upsert`), done은 `done=true`로 같다 | E7(재시도는 새 진행) 정책은 바꾸지 않는다 |
| A10 | 비밀값 | `.env`·Secret 값, 환경 변수 이름은 넣지 않는다. `env_keys`는 개수만 | Issue 결정 |
| A11 | migration_id | `AnalysisInput.migration_id` = 부모 `MigrationWorkflow` ID(부모가 넣는다) | `workflows/migration.py` |
| A12 | 발행 실패 | 항목 계산·저장소 읽기 오류는 운영 경고만 남기고 분석을 실패시키지 않는다(E9) | 관찰은 실행을 막지 않는다 |

### key별 발행 위치

| key | stream | via | Activity | 값(detail) |
| --- | --- | --- | --- | --- |
| commit | code | code | fetch_source | `1 · <커밋 12자>` |
| files | code | code | fetch_source | 작업 공간 파일 수(`.git` 제외) |
| manifests | code | config | scan_manifests | 근거가 나온 매니페스트 파일 수 |
| services | code | config | scan_manifests | Deployment 수 |
| statefulsets | code | config | scan_manifests | StatefulSet 수 |
| health_endpoint | code | config | scan_manifests | HTTP 프로브 경로 수 · 경로(최대 3) |
| db_driver | code | code | scan_code | DB 드라이버 수 · 모듈(최대 3) |
| env_keys | code | code | scan_code | 코드가 읽는 환경 변수 이름 수(이름은 보내지 않음) |
| resources | code | config | build_requirements | Requirements 리소스 수 · 종류별 수 |
| eks_cluster | infrastructure | config | scan_aws_infra | `1 · <이름> <버전>` |
| vpc | infrastructure | config | scan_aws_infra | VPC 수 · CIDR(최대 3) |
| subnet | infrastructure | config | scan_aws_infra | 서브넷 수 |
| security_group | infrastructure | config | scan_aws_infra | 보안 그룹 수 |
| alb | infrastructure | config | scan_aws_infra | `type=application` 로드밸런서 수 |
| iam_role | infrastructure | iam | scan_aws_infra | 정책을 읽은 IAM 역할 수 |
| trail_calls | infrastructure | trail | scan_aws_infra | CloudTrail 호출 합계 |
| node·nat_gateway·ebs_gb | infrastructure | — | — | 값 없음, 내지 않음(A4) |
| code-done·infrastructure-done | 각 흐름 | code·config | check_evidence | progress 100, 열에 줄로 나오지 않는 종료 표시 |

`scan_aws_infra`가 `NOT_COLLECTED`·`FAILED`면 infrastructure key는 없고 종료 표시만 100으로 온다.

## 수용 기준
- [x] 각 이벤트가 계약 모델(`AnalysisFindingData`·`AnalysisDoneData`)로 검증된다.
- [x] 같은 시도 안 재실행은 이벤트를 늘리지 않는다. 한 실행에서 finding `id`가 겹치지 않고 done은 1건이다.
- [x] 비밀값·환경 변수 이름이 data에 없다.
- [x] 스캔이 실패하면 그 스캔의 finding과 done을 내지 않는다.
- [x] migration_id가 부모 MigrationWorkflow ID다(Temporal 테스트 서버 통합 테스트).

## Test Seam
`AnalysisActivities`를 Temporal `ActivityEnvironment`에서 실행하고 `configure_emitter(InMemoryEmitter())`로 저장된 이벤트를 본다([tests/analysis/test_analysis_events.py](../../tests/analysis/test_analysis_events.py)). 부모 ID는 `MigrationWorkflow` 통합 테스트([tests/analysis/test_pipeline_wiring.py](../../tests/analysis/test_pipeline_wiring.py)).

## 검증
- `make check`, `uv run alembic check`(스키마 변경 없음), `make openapi`(HTTP 변경 없음, 차이 없음)

## 남은 일
- 웹: finding 방식에서 새 key(`commit`·`eks_cluster` 등)의 이름 문구(`analysis.node.<key>.name`·`analysis.u.<key>.*`)가 없어 key 그대로 보인다. 같은 key의 `analysis.item.<key>` 문구는 이미 있어, 웹이 finding 줄에도 그것을 쓰면 된다(웹 저장소, 별도).
- 사람 검토 게이트(`confirm_requirements`·`approve_losses`)를 보낼 HTTP 경로가 아직 없어, 매핑 행이 나오면 `analysis.done` 뒤에도 Workflow가 검토를 기다린다(분석 담당 후속).
- 실제 서버(데모 환경)에서 SSE로 화면이 채워지는지 확인.
