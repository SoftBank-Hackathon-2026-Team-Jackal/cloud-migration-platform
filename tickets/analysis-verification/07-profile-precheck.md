# Ticket 07 — 사용 프로필·사전 점검·분석 결과 조립

## 상태
DRAFT — 사용자 검토 전

## 목적
스캔 결과를 사용 프로필로 합치고 Dockerfile·빌드 컨텍스트와 DB 사전 점검을 더해 `AnalysisResult` 초안을 만든다.

## 관련 요구사항
AV-006, AV-017

## 선행 작업
Blocked by: 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시)

## 작업 범위
- 포함: 결과 병합, 교차 해석(코드·CloudTrail·권한), Dockerfile 파싱과 `COPY`·`ADD` 경로 확인, 사전 점검 쿼리(기본 키 없는 테이블·확장·시퀀스·`wal_level`·세션 기능), 환경변수 분류(일반·동적 바인딩·비밀 이름)
- 제외: 매핑 판단(08)

## 수용 기준
- [ ] Dockerfile 유무·위치와 누락된 `COPY` 경로가 기록된다.
- [ ] 로컬 PostgreSQL fixture에서 기본 키 없는 테이블·확장·시퀀스가 감지된다.
- [ ] `DB_HOST`·`DB_PORT` 등이 동적 바인딩으로, `DB_PASSWORD`가 비밀(이름·분류)로 분류된다.
- [ ] 결과가 01 계약 검증을 통과한다.

## 테스트 전략
- 제안 Test Seam: `build_profile(...)`·`precheck(...)` 결과. 잡는 것: 병합·분류 규칙. 못 잡는 것: 실제 원본 DB 접근 권한.
- 실행: fixture + 로컬 Postgres(`infra/local/compose.yaml`), `uv run pytest tests/analysis/test_profile.py`

## 완료 시 확인할 동작
스캔 fixture로 완성된 `AnalysisResult`(매핑 제외)가 나온다.

## 결정 대기
- 원본 PostgreSQL이 RDS인지 Pod인지(소울님)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
