# Ticket 07 — 요구사항·사용 프로필·사전 점검 조립

## 상태
DRAFT — 사용자 검토 전

## 목적
스캔 결과를 `RequirementsV2` 요구사항과 사용 프로필로 합치고, 환경변수 예외 목록·코드 수준 사전 점검을 만든다.

## 관련 요구사항
AV-006, AV-017

## 선행 작업
Blocked by: 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시)

## 작업 범위
- 포함: 결과 병합, 교차 해석(코드·CloudTrail·권한), `RequirementsV2` 생성(`resources[]`, DB 연결 `env_bindings`, `network_intent`), 확장 요구 시 `BLOCK` 보고, `EnvExceptions`(제외·대체·누락), Dockerfile·빌드 컨텍스트 점검, 코드 수준 사전 점검
- 제외: 매핑 판단(08), DB 내부 상태 점검(D-13)

## 수용 기준
- [ ] 요구사항에 `APPLICATION`·`POSTGRESQL` 리소스와 DB 연결 `env_bindings`가 나오고 `RequirementsV2` 검증을 통과한다.
- [ ] 앱이 PostgreSQL 확장을 요구하면 요구사항은 비워 두고 해당 기능이 `BLOCK`으로 보고된다.
- [ ] 원본 환경에 묶인 변수(`AWS_REGION` 등)가 `excluded`로, 누락 변수가 `missing`으로 나온다.
- [ ] Dockerfile 유무·위치와 누락된 `COPY` 경로가 기록된다.
- [ ] 세션 단위 기능·확장 사용·헬스체크 경로·노출된 클라우드 주소가 사전 점검에 나온다.

## 테스트 전략
- 제안 Test Seam: `build_requirements(...)`·`precheck(...)` 결과. 잡는 것: 병합·연결·분류 규칙. 못 잡는 것: 실제 원본 DB 상태.
- 실행: fixture, `uv run pytest tests/analysis/test_requirements.py`

## 완료 시 확인할 동작
스캔 fixture로 완성된 요구사항·`AnalysisReport`(매핑 제외)·예외 목록이 나온다.

## 결정 대기
- 확장 지원(D-14)
- DB 내부 상태 점검 담당(D-13)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리소스 목록 요구사항, 예외 목록 반영. DB 내부 상태 점검은 D-13으로 분리. v2 동기화로 요구사항을 `RequirementsV2`로 바꾸고 DB 이미지 확정 규칙 삭제(이미지 선택은 D-11).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
