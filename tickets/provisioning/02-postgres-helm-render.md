# Ticket 02 — PostgreSQL Chart 렌더링과 PVC 검사

## 상태
DONE — Issue #11; 구현·로컬 검증 완료, main 반영은 구현 PR 병합으로 확인

## 목적
공통 요구에서 PostgreSQL 16 StatefulSet·Service·PVC를 검증 가능한 Manifest로 만든다.

## 관련 요구사항
PROV-005, PROV-006, PROV-011

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: 공통 Helm Chart/Values, DB 자원 요구, Secret 참조 자리와 StorageClass/PVC 렌더 검사를 구현한다.
- 제외: 실제 데이터 복사·CDC, Production 클러스터 설치

## 수용 기준
- [x] 세 대상 Values로 PostgreSQL 16 StatefulSet·Service·PVC가 렌더된다.
- [x] Pod CPU/메모리와 PVC 용량이 서로 다른 항목으로 반영되고 잘못된 참조는 실패한다.
- [x] Sandbox Values는 Production 자격 증명 없이 같은 Chart를 렌더한다.

## 테스트 전략
- 제안 Test Seam: 공개 Helm 렌더 산출물. 실제 PVC Bound는 확인하지 못한다.
- 실행: `helm lint`, `helm template` 및 Manifest 구조 fixture; 가능하면 `make check`.

## 완료 시 확인할 동작
대상별 DB Manifest와 Sandbox 렌더 결과를 비교할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: DB/App 별도 Chart/Release로 결정. StorageClass·Secret 이름/키는 명시 입력이며 Provider 공통 기본값을 임의 지정하지 않는다.
- 구현 중 구체화: 단일 DB StatefulSet·headless Service·별도 보존 PVC. CPU/메모리는 requests/limits에 동일 적용한다. 공유 입력에서 생성한 Helm schema를 사용하고 해결되지 않은 Registry 인증 참조는 거절한다. 정책 변경 없음; 실배포·Registry 인증 연결은 후속 티켓 범위다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

담당: PostgreSQL Chart·Values 렌더·전용 테스트. Sandbox와 Production은 같은 DB Chart를 사용하고 서로 다른 Values를 전달한다.

## 검증 증거

- Issue: [#11](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/11).
- RED: Chart 미구현 상태에서 정상 렌더/스키마 검사 7개 실패 확인.
- GREEN: 실제 Helm 3.17.3 `lint`/`template` 공개 경계 테스트 21개 통과. 세 Provider × Sandbox/Production 6조합, 단위·참조·이미지 Digest·추가 비밀번호 필드 거절·Release 불일치·아키텍처/YAML 문자열 보존 확인.
- `HELM_BIN=/tmp/cmp-tools/darwin-arm64/helm UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 및 전체 134개 통과. 기존 Starlette deprecation warning 1개.
- 자체 리뷰: Secret 원문/클라우드 Credential 없음, 명시 PVC/StorageClass, Pod와 Service selector 및 볼륨 참조 일치, 공유 schema 정합성, CI Helm 버전 고정 확인.
- 실제 클러스터 설치/PVC Bound/Secret 실존/DB 접속/이미지 PostgreSQL 버전 실행 검증은 수행하지 않았다. 수용 범위는 렌더이며 실제 배포 검증을 완료로 표시하지 않는다.
- HTTP/Temporal 코드 변경 없음: OpenAPI 재생성/Temporal smoke 대상 아님.
- [Helm 사용법과 제한](../../infra/helm/README.md). 문서 수정은 동일 구현 PR에 포함한다.
