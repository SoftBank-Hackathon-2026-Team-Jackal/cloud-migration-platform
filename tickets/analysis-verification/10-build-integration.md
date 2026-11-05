# Ticket 10 — Build 모듈 연동과 실패 분류

## 상태
IN_PROGRESS — 구현 완료(가짜 Build 모듈 기준), 리뷰 대기. Issue 번호는 발행 후 기입

## 목적
공통 Build 모듈을 호출하고 결과를 재사용·분류해 루프에 넘긴다.

## 관련 요구사항
AV-010

## 선행 작업
Blocked by: 09, Provisioning B-01(DONE)

## 작업 범위
- 포함: `request_build`(`BuildContext(resource_id 포함)`·`ImageBuilder.build(source_uri, source_version_id=...)` 함수 호출, D-12에 따라 호출 위치를 바꿀 수 있게 감쌈), 플랫폼은 capabilities 아키텍처, `attempt_id` 규칙(`<run>-b<n>`), `(resource_id, source_sha256, platform)` 재사용 기록, Build 결과 불변 저장과 `platforms[]` 정규화, 실패 코드 분류표, Heartbeat
- 제외: Build 모듈 수정(민서님 영역)

## 수용 기준
- [x] 같은 소스 해시·플랫폼으로 두 번 요청하면 두 번째는 Build 모듈을 부르지 않고 같은 Digest를 쓴다.
- [x] `DOCKERFILE_MISSING`은 수정 대상, 아카이브 오류 코드는 재패키징, `ECR_*`는 재시도 후 사람 검토로 분류된다.
- [x] `EXTERNAL_COMMAND_FAILED`는 같은 `attempt_id`로 1회 재시도 후 빌드 실패로 처리된다.
- [x] 성공 결과의 `image_uri`가 `repo@sha256:`이고 `image_digest`와 일치하며, `source_ref`가 요청한 아카이브와 같다.
- [x] 저장한 Build 결과가 `BuildEvidenceV2`(`result_ref`, `platforms[]`) 형태로 바뀐다.
- [x] 자격 증명이 Activity 인자·결과·이력에 없다.

## 테스트 전략
- 합의된 Test Seam: `ImageBuilder`(또는 Activity)를 가짜로 바꾼 `request_build` 결과 + 로컬 Registry 1회 실제 빌드. 잡는 것: 재사용·분류. 못 잡는 것: 실제 ECR 권한·빌드 시간.
- 실행: `uv run pytest tests/sandbox/test_build.py`, 로컬 Registry 실행 기록

## 완료 시 확인할 동작
fixture 아카이브로 Digest 고정 이미지 참조가 나오고 실패 코드별 다음 행동이 정해진다.

## 검증 증거
- `uv run pytest tests/sandbox/test_build.py`: 26 통과. 같은 `(resource_id, source_sha256, platform)`은 Build 모듈을 다시 부르지 않고 같은 Digest 재사용, 다른 플랫폼·리소스·소스는 다시 빌드, 실패는 재사용하지 않음, 실패 코드 분류표(`DOCKERFILE_MISSING`→수정, 아카이브 오류→재패키징, `ECR_*`·로그 업로드→재시도 후 검토, `EXTERNAL_COMMAND_FAILED`·`BUILD_DIGEST_*`→빌드 실패, 모르는 코드→검토), `EXTERNAL_COMMAND_FAILED`는 같은 `attempt_id`로 1회 재시도 후 빌드 실패, 다른 실패는 재시도 안 함, Build 모듈 호출 인자(아카이브 URI·버전·컨텍스트), 성공 결과 검증(`repo@sha256:` 일치, 요청한 아카이브·플랫폼 일치, 어긋나면 `BUILD_RESULT_INCONSISTENT`), `BuildEvidenceV2`로 정규화(`platforms[]`, 내용에서 만든 `result_ref`), `attempt_id` 규칙 `<run>-b<n>`, 자격 증명 필드 거부, 실행 중 Heartbeat, 저장 결과 불변을 확인했다.
- 구현: `apps/worker/src/cmp_worker/build_request.py`(`request_build`, `classify_failure`, `to_build_evidence`, `BuildTarget`, `InMemoryBuildStore`). Build 모듈은 `builder_factory`로만 호출해 D-12에 따라 위치를 바꿀 수 있다. `image_build.py`는 수정하지 않았다.
- Helm이 필요한 테스트 파일을 제외한 `pytest`와 `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 테스트가 찾은 문제: 저장 결과의 불변 키를 `(resource_id, attempt_id)`로 잡았더니 한 시도에서 두 플랫폼을 빌드할 때 충돌해서 `(resource_id, attempt_id, platform)`으로 고쳤다(B-01의 이미지 태그가 플랫폼을 포함한다).
- 문서 변경: spec·plan 변경 없음(§2.5와 같은 구조).

## 한계와 미검증
- 실제 Build 모듈(ECR·Docker)을 부르지 않았다. 가짜 빌더로만 확인했고, "로컬 Registry 1회 실제 빌드" 기록은 Docker와 로컬 Registry가 있는 환경에서 따로 남겨야 한다. 실제 ECR 권한과 빌드 시간은 검증하지 못했다.
- 결과 저장은 메모리 구현(`InMemoryBuildStore`)이다. 실제 불변 저장 위치와 정규화 Activity 담당은 D-02이다. 호출 Worker(권한 배치)와 전체 시간 상한은 D-12이다.
- `EXTERNAL_COMMAND_FAILED` 재시도 외의 재시도 횟수(`ECR_*`·로그 업로드는 연속 2회면 사람 검토)는 14의 루프가 센다. 이 티켓은 분류와 다음 행동만 낸다.
- 02의 Activity 연결(Heartbeat 포함)과 09 아카이브 결과를 넘기는 통합은 별도 작업이다. 플랫폼은 대상 capabilities에서 가져오는 연결이 아직 없다.

## 결정 대기
- D-12 호출 Worker(권한 배치)·전체 시간 상한
- D-04 빌드 실패 상세(급하지 않음)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: capabilities 기반 플랫폼, 호출 방식 미정(D-12) 반영. v2 동기화로 `resource_id`·`source_version_id`·새 반환 필드 반영.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
