# Ticket 04 — 격리 K3s에서 Agent 배포 경로 검증

## 상태
DONE — 사용자 범위 승인, Issue #15; main 반영은 구현 PR 병합으로 확인

## 목적
Agent가 기존 K3s의 격리된 테스트 Namespace에 DB·앱을 배포하고 상태를 보고한다.

## 관련 요구사항
PROV-006, PROV-009, PROV-011, PROV-013

## 선행 작업
Blocked by: 02, 03

## 작업 범위
- 포함: Agent 작업 ID·대상 클러스터/Namespace·Helm 산출물 참조, 연결·상태 조회와 테스트 리소스 정리를 구현한다.
- 제외: Production 승인 흐름과 외부 Envoy·WireGuard 구성

## 수용 기준
- [x] Agent가 연결·클러스터 접근·StorageClass 가능 여부를 보고한다.
- [x] 테스트 Namespace에서 DB PVC Bound·Pod Ready·앱 Service 연결을 확인한다.
- [x] 동일 작업 ID 재전달과 Agent 재연결 시 기존 작업 상태를 조회한다.

## 테스트 전략
- 제안 Test Seam: Agent 작업 제출/조회 및 격리 K3s 상태. 클라우드 대상은 확인하지 못한다.
- 실행: Agent 상태 계약 테스트와 격리 K3s 통합 테스트; 리소스 정리 증거 기록; `make check`.

## 완료 시 확인할 동작
격리 K3s에서 실제 DB·앱 배포 결과와 작업 ID를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 사용자 결정: 별도 k3d 환경에서 Agent 폴링과 실제 배포를 검증. 학습용 minikube 제외.
- 구현 구체화: 신뢰 로컬 SQLite spool을 별도 Agent 프로세스가 폴링한다. 원격 통신·등록·인증 및 운영 K3s 준비는 후속 통합 범위다. UID/작업 라벨로 테스트 대상 고정, Helm 렌더 Manifest Digest 검사, 불명 상태는 재적용 없이 조회한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 검증 증거

- Issue #15: [구현·재현·책임 경계](../../specs/provisioning/k3s-agent.md), [실제 K3s 결과](../../docs/verification/provisioning-04-k3s.json).
- RED: 영속 폴링 공개 경계 미구현 실패 확인. GREEN: Agent 테스트 18개 통과. 동일 ID 변경 거절·원자적 claim·불명 실행 재발행 방지·잘못된 대상/소유권/산출물 거절·drift 검사 확인.
- `HELM_BIN=/tmp/cmp-tools/darwin-arm64/helm UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 및 전체 186개 통과(기존 Starlette 경고 1개).
- 실제 K3s 1.31.5-k3s1 / ARM64에서 DB PVC Bound·DB/App Ready·Service를 통한 인증된 SQL 쓰기/읽기 통과. 테스트 Namespace smoke-0a510327, 기본 kubeconfig 바이트 보존.
- 중복 전달 후 IDLE, 새 Agent 프로세스 재연결 후 기존 성공 상태 조회, 완료 응답 유실 주입 후 실제 리소스/Manifest 비교로 복구. DB Pod UID 불변으로 재배포 없음 확인.
- 테스트 Namespace·PVC·전용 k3d 클러스터·Registry 정리 완료. Registry는 클러스터 삭제와 함께 제거됐다. 기본 minikube 컨텍스트 및 실행 중 minikube 컨테이너 유지.
- 자체 리뷰: 명시 kubeconfig/context, cluster/namespace UID·작업 소유권, 두 산출물 선검증, 비밀값 stdin 처리·오류 원문 미기록, 운영 미구현 경계 확인.
- 원격 Agent 인증/네트워크 재연결·운영 lease/동시 실행 조정·Helm Release 관리·Production 승인 연결·AWS/GCP 검증은 완료로 표시하지 않는다. 현재 실증 범위는 로컬 폴링 Agent와 실제 격리 K3s다.
- HTTP/Temporal 코드 변경 없음. OpenAPI 재생성/Temporal smoke 대상 아님.
