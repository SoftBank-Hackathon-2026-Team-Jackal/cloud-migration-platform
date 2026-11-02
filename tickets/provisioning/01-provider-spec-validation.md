# Ticket 01 — 공통 입력 검증과 대상별 구성 예시

## 상태
IN_PROGRESS — 입력 계약 문서 정리 착수; 코드 구현·연동 검증은 미완료

GitHub Issue: [#5](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/5)

2026-11-03 사용자 요청으로 입력 정보 구성·기획 반영·Issue 생성에 착수했다. 이번 완료 범위는 문서 정리이며, 티켓 전체 수용 기준 완료와 구분한다.

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
- [ ] AWS·GCP는 자격 증명 참조·Region·Node/네트워크 요구를, ON_PREM은 Agent·기존 K3s 정보를 검증한다.
- [ ] Analysis·Sandbox·Build와 사용자 선택의 참조/버전을 조합해 Spec을 만들며, 필수 선행 산출물이 빠지거나 서로 맞지 않으면 출처가 드러나는 오류를 반환한다.
- [ ] 지원하지 않는 Provider, CIDR 충돌, 이미지·Node 아키텍처 불일치를 항목별 오류로 반환한다.
- [ ] 동일 앱·DB 요구에서 Provider별 구성 차이를 확인하고 객체 스토리지를 필수 입력으로 요구하지 않는다.

## 테스트 전략
- 제안 Test Seam: 선행 결과 참조 → `ProvisioningSpec` 조합·검증 및 공개 Adapter 결과. 앞 단계의 실제 구현과 Provider 권한은 검증하지 못한다.
- 실행: 선행 산출물 누락·버전 불일치와 정상 3개 대상, 필수값 누락·CIDR·아키텍처 fixture를 RED→GREEN으로 검증; `make check`.

## 완료 시 확인할 동작
선행 단계별 입력 출처와 Spec 조합 결과, 세 대상의 구성 요약·오류 목록을 로컬에서 재현할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 앞 단계 출력 모델·전달 방식, 필드명·기본값·Node 선정 정책이 확정되지 않으면 제안으로 유지하고 담당 영역과 합의한다.
- 2026-11-03 (#5): [Provision 입력 계약](../../specs/provisioning/input-contract.md)을 추가했다. Sandbox/Build 출처 연결, Temporal/S3 역할, Node/PVC 분리, 환경별 동적 바인딩, 미정 정책을 명시하고 spec/plan/아키텍처에 연결했다. 제품 정책·HTTP 계약·ADR-001은 변경하지 않았다.
- 코드 테스트 경계는 위 제안을 유지한다. 상위 팀 wire schema와 Digest 산정 범위를 합의하기 전 임의 필드를 확정 계약으로 구현하지 않는다.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 이번 문서 작업 검증

- `git diff --check`: 통과 (2026-11-03).
- 변경 문서 7개의 Markdown 상대 파일 링크 검사 통과. 제품 정책/PROV 요구사항과 코드·이미지 추적, Node/PVC 및 승인 경계 정합성 검토 완료.
- Python/HTTP/Temporal 코드 변경 없음. `make check`, `make openapi`, `make smoke-temporal`은 이번 문서 작업에서 실행하지 않는다.
- 실제 Worker 연결·S3 조회·클라우드 배포·티켓의 동작 테스트는 미검증이다.
