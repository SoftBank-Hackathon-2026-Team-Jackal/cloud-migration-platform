# Ticket 10 — Build 모듈 연동과 실패 분류

## 상태
DRAFT — 사용자 검토 전

## 목적
공통 Build 모듈을 호출하고 결과를 재사용·분류해 루프에 넘긴다.

## 관련 요구사항
AV-010

## 선행 작업
Blocked by: 09, Provisioning B-01(DONE)

## 작업 범위
- 포함: `request_build`(현재는 `BuildContext`·`ImageBuilder` 함수 호출, D-12에 따라 Activity 호출로 교체 가능하게 감쌈), 플랫폼은 capabilities 아키텍처, `attempt_id` 규칙(`<run>-b<n>`), `(source_sha256, platform)` 재사용 기록, 실패 코드 분류표, Heartbeat
- 제외: Build 모듈 수정(민서님 영역)

## 수용 기준
- [ ] 같은 소스 해시·플랫폼으로 두 번 요청하면 두 번째는 Build 모듈을 부르지 않고 같은 Digest를 쓴다.
- [ ] `DOCKERFILE_MISSING`은 수정 대상, 아카이브 오류 코드는 재패키징, `ECR_*`는 재시도 후 사람 검토로 분류된다.
- [ ] `EXTERNAL_COMMAND_FAILED`는 같은 `attempt_id`로 1회 재시도 후 빌드 실패로 처리된다.
- [ ] 성공 결과의 `image_uri`가 `repo@sha256:`이고 `image_digest`와 일치한다.
- [ ] 자격 증명이 Activity 인자·결과·이력에 없다.

## 테스트 전략
- 제안 Test Seam: `ImageBuilder`(또는 Activity)를 가짜로 바꾼 `request_build` 결과 + 로컬 Registry 1회 실제 빌드. 잡는 것: 재사용·분류. 못 잡는 것: 실제 ECR 권한·빌드 시간.
- 실행: `uv run pytest tests/sandbox/test_build.py`, 로컬 Registry 실행 기록

## 완료 시 확인할 동작
fixture 아카이브로 Digest 고정 이미지 참조가 나오고 실패 코드별 다음 행동이 정해진다.

## 결정 대기
- D-12 호출 방식(함수 vs Activity)·전체 시간 상한·`resource_id`·`source_version_id`
- D-04 빌드 실패 상세(급하지 않음)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: capabilities 기반 플랫폼, 호출 방식 미정(D-12) 반영.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
