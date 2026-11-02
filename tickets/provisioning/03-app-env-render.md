# Ticket 03 — 앱 배포와 .env·Secret 바인딩 렌더링

## 상태
DRAFT

## 목적
검증된 이미지와 별도 `.env` 참조를 앱 Deployment·설정 참조로 변환한다.

## 관련 요구사항
PROV-004, PROV-005, PROV-010, PROV-012, PROV-021

## 선행 작업
Blocked by: 01, 02

## 작업 범위
- 포함: 이미지 Digest·Pod 자원·헬스·Service, 정적/동적 변수와 사용자/생성 Secret의 바인딩 계획을 구현한다.
- 제외: 실제 Secret 제품 선정과 클러스터 배포

## 수용 기준
- [ ] 앱 Deployment가 이미지 Digest·포트·헬스·자원을 렌더한다.
- [ ] DB_HOST/PORT 등 생성 후 값의 바인딩 위치와 Secret 참조를 검사한다.
- [ ] Preview·렌더 로그·검증 오류에 `.env` 원문·비밀번호가 나타나지 않는다.

## 테스트 전략
- 제안 Test Seam: 환경 바인딩 계획과 렌더 Manifest. 실제 Secret 조회 권한은 확인하지 못한다.
- 실행: 대표 `.env` 키 메타데이터·누락 키·동적 DB Endpoint fixture, `helm lint`/`template`, `make check`.

## 완료 시 확인할 동작
Sandbox/Production Values가 달라도 같은 Chart로 앱/DB 참조를 렌더한다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Secret 저장·회전 방식과 Registry Pull 인증은 별도 결정이 필요하다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
