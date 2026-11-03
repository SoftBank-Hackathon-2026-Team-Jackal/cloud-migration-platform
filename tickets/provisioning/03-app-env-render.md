# Ticket 03 — 앱 배포와 .env·Secret 바인딩 렌더링

## 상태
DONE — 사용자 범위·Test Seam 승인(2026-11-03), Issue #13. main 반영은 구현 PR 병합으로 확인.

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
- [x] 앱 Deployment가 이미지 Digest·포트·헬스·자원을 렌더한다.
- [x] DB_HOST/PORT 등 생성 후 값의 바인딩 위치와 Secret 참조를 검사한다.
- [x] Preview·렌더 로그·검증 오류에 `.env` 원문·비밀번호가 나타나지 않는다.

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
- 구현 구체화: 일반 설정도 ConfigMap 이름/키로 받는 참조 전용 입력. 공통 DynamicBinding의 host/secret_name 두 슬롯을 재사용하며 나머지 DB 연결 메타데이터는 계획에 고정한다. Secret 제품·회전·조회·생성은 범위 밖이다. 해결되지 않은 Registry 인증 참조는 02와 동일하게 거절한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 구현·검증 증거

- [Issue #13](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/13), [입력·호출 예제](../../specs/provisioning/application-rendering.md).
- RED→GREEN: 공개 계획 함수 부재 실패 → 명시 매핑 계획 통과, 렌더 함수 부재 실패 → 실제 Helm Manifest 검증 통과.
- Helm 3.17.3: 앱 테스트 34개. AWS/GCP/ON_PREM × Sandbox/Production lint/template, ConfigMap/사용자 Secret/생성 DB Secret 매핑, 누락·중복·미해결 값·승인 범위 외 변조 거절, 원문 입력/Helm 오류 비노출을 확인했다.
- `HELM_BIN=/tmp/cmp-tools/darwin-arm64/helm UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 및 전체 168개 통과. 기존 Starlette deprecation warning 1개.
- 자체 리뷰: 공통 바인딩 재사용, input 불변성, Chart/schema 정합성, DB와 App Release 분리, Secret 원문 미수신, Workflow 외부 I/O 금지 경계 확인.
- HTTP/Temporal 변경 없음. OpenAPI 재생성/Temporal smoke 대상 아님.
- 클러스터·ConfigMap/Secret 생성/조회 권한·실제 DB/앱 연결·이미지 실행·Ingress·원격 Artifact 저장은 미검증/미구현이며 이 티켓의 완료 범위가 아니다. 업스트림 실제 wire schema 연결은 별도 변환 경계다.
