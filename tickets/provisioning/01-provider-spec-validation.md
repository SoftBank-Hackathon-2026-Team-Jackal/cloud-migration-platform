# Ticket 01 — 공통 입력 검증과 대상별 구성 예시

## 상태
DONE — 내부 계약·Planning·Provider 검증 및 로컬 회귀 완료 (2026-11-03)

GitHub Issue: [#5](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/5)

2026-11-03 사용자 요청으로 입력 정보 구성·기획 반영·Issue 생성에 착수했다. 후속 사용자 요청으로 티켓 전체의 내부 계약·검증·구성 예시까지 구현 범위를 진행했다.

## 목적
앞 단계의 결과와 사용자 선택이 `ProvisioningSpec`으로 어떻게 조합되는지 정한 뒤, AWS·GCP·ON_PREM별 필수 입력과 변환 결과를 확인한다.

## 관련 요구사항
PROV-001, PROV-002, PROV-003

## 선행 작업
Blocked by: 없음

## 작업 범위
- 포함: API/Analysis/Sandbox/Build에서 받아야 할 참조·결과의 최소 계약과 출처를 확인하고, Planning의 `ProvisioningSpec` 조합 경계를 정의한다. 그 계약에 맞춰 최소 Pydantic v2 검증, Provider별 분기, Node/Pod 구분, 검증 오류를 구현한다.
- 제외: Terraform/Helm 실행과 자격 증명 원문 조회

## 수용 기준
- [x] 선행 결과 수신·Spec 조합·승인 후 실행의 세 입력 경계와 정보 출처를 문서화한다.
- [x] AWS·GCP는 자격 증명 참조·Region·Node/네트워크 요구를, ON_PREM은 Agent·기존 K3s 정보를 검증한다.
- [x] Analysis·Sandbox·Build와 사용자 선택의 참조/버전을 조합해 Spec을 만들며, 필수 선행 산출물이 빠지거나 서로 맞지 않으면 출처가 드러나는 오류를 반환한다.
- [x] 지원하지 않는 Provider, CIDR 충돌, 이미지·Node 아키텍처 불일치를 항목별 오류로 반환한다.
- [x] 동일 앱·DB 요구에서 Provider별 구성 차이를 확인하고 객체 스토리지를 필수 입력으로 요구하지 않는다.

## 테스트 전략
- 확정 Test Seam (이슈 전체 구현 요청에 따른 공개 경계): 선행 결과 참조 → `ProvisioningSpec` 조합·검증 및 공개 Adapter 결과. 앞 단계의 실제 구현과 Provider 권한은 검증하지 못한다.
- 실행: 선행 산출물 누락·버전 불일치와 정상 3개 대상, 필수값 누락·CIDR·아키텍처 fixture를 RED→GREEN으로 검증; `make check`.

## 완료 시 확인할 동작
선행 단계별 입력 출처와 Spec 조합 결과, 세 대상의 구성 요약·오류 목록을 로컬에서 재현할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 앞 단계 출력 모델·전달 방식, 필드명·기본값·Node 선정 정책이 확정되지 않으면 제안으로 유지하고 담당 영역과 합의한다.
- 2026-11-03 (#5): [Provision 입력 계약](../../specs/provisioning/input-contract.md)을 추가했다. Sandbox/Build 출처 연결, Temporal/S3 역할, Node/PVC 분리, 환경별 동적 바인딩, 미정 정책을 명시하고 spec/plan/아키텍처에 연결했다. 제품 정책·HTTP 계약·ADR-001은 변경하지 않았다.
- 내부 v1 모델을 공개 Planning/Adapter 경계에 구현했다. 상위 팀 wire schema와 구분하며 조회 Activity가 정규화하도록 설계했다. 세부 모델/제한은 입력 계약 §10에 기록한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 초기 문서 작업 검증

- `git diff --check`: 통과 (2026-11-03).
- 변경 문서 7개의 Markdown 상대 파일 링크 검사 통과. 제품 정책/PROV 요구사항과 코드·이미지 추적, Node/PVC 및 승인 경계 정합성 검토 완료.
- Python/HTTP/Temporal 코드 변경 없음. `make check`, `make openapi`, `make smoke-temporal`은 이번 문서 작업에서 실행하지 않는다.
- 실제 Worker 연결·S3 조회·클라우드 배포·티켓의 동작 테스트는 미검증이다.

## 최종 구현·검증 증거 (2026-11-03)

- 사용자 후속 요청: 문서에 한정하지 않고 Issue #5 범위 전체 구현·검증·커밋 수행.
- `cmp_contracts.provisioning`: 엄격한 내부 v1 계약, Provider 판별 모델, 버전/Digest 참조 및 안전한 항목별 오류.
- `cmp_worker.provisioning.prepare_plan`: Analysis/Sandbox/Build·사용자 선택 조합, 상태·작업·출처·버전 검증, 미정 정리 정책의 명시적 반환.
- `cmp_providers.provisioning.adapt`: EKS/GKE/K3s 구성 요약, Node/Pod/PVC 구분, Provider 네트워크·CIDR·이미지 아키텍처 검사.
- 독립 fixture 3개로 동일 앱/DB의 AWS/GCP/ON_PREM 정상 결과를 재현했다. P0 객체 스토리지 생략 가능하며 생성 요청을 조용히 수락하지 않는다.
- RED 확인: 진입점 부재, Build 소스 불일치, CIDR 중복, 이미지/Node 불일치, 정리 정책 결과 미보존. 각 구현 후 GREEN 확인.
- `uv run pytest tests/test_provisioning.py -q`: 51 passed.
- `UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 통과, 전체 54 passed (기존 scaffold 회귀 3개 포함). 기존 Starlette/httpx deprecation warning 1개.
- 최초 sandbox 내 uv 실행은 macOS system-configuration panic으로 중단됐으며, 정상 권한 환경에서 같은 필수 명령을 재실행해 통과했다.
- 자체 리뷰: 선행 결과 연결의 누락/불일치, 비밀값 오류 노출, AWS/GCP 네트워크 구분, 기존 네트워크 재사용, 미정 정책의 임의 확정 여부 및 SDD 정합성 검토 완료. 정리 실패는 강제 성공/실패 정책 대신 pending_decisions로 보존하도록 수정했다.
- 실제 상위 Worker 실행·S3 조회/무결성 계산·권한·클라우드 CIDR/인스턴스 카탈로그·배포는 이 티켓의 로컬 seam 밖이며 미검증이다. 검증 성공을 실제 클라우드 지원으로 주장하지 않는다.
- HTTP/Temporal 실행 코드·Terraform/Helm 변경 없음: OpenAPI 재생성·Temporal smoke·Cloud Apply는 비대상. 실제 생성 자원 없음.
- 문서와 구현 차이는 입력 계약 §10에 기록했고, 후속 티켓의 범위를 이번 완료로 표시하지 않았다.
