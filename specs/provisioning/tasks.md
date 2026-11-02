# Tasks: Provisioning

이 파일은 7단계 Provisioning 티켓 **초안 인덱스**다. 각 티켓의 상세 수용 기준·Test Seam·검증 방법은 연결된 파일에 있다. 01은 사용자 요청(2026-11-03, #5)에 따라 입력 계약·로컬 검증·대상별 구성 예시와 회귀 검증을 완료했다. 나머지는 `DRAFT`이며 사용자 검토 전 실제 구현을 시작하지 않는다. 상태는 `DRAFT → TODO → IN_PROGRESS → DONE`을 기본으로 하고, 선행 작업이나 결정이 막히면 `BLOCKED`로 기록한다. 검토 후 구현 전 GitHub Issue를 생성하거나 기존 Issue를 연결하고 아래 번호를 기록한다. `DONE`은 [개발 워크플로](../../docs/engineering/development-workflow.md)의 문서·테스트·리뷰 조건을 모두 충족한 경우에만 사용한다.

| ID·티켓 | 상태 | Blocked by | 관련 요구사항 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [01 공통 입력 검증과 대상별 구성 예시](../../tickets/provisioning/01-provider-spec-validation.md) | DONE | 없음 | PROV-001, 002, 003 | [#5](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/5) | ☑ |
| [02 PostgreSQL Chart 렌더링과 PVC 검사](../../tickets/provisioning/02-postgres-helm-render.md) | DRAFT | 01 | PROV-005, 006, 011 | 미생성 | ☐ |
| [03 앱 배포와 .env·Secret 바인딩 렌더링](../../tickets/provisioning/03-app-env-render.md) | DRAFT | 01, 02 | PROV-004, 005, 010, 012, 021 | 미생성 | ☐ |
| [04 격리 K3s에서 Agent 배포 경로 검증](../../tickets/provisioning/04-k3s-agent-test-deploy.md) | DRAFT | 02, 03 | PROV-006, 009, 011, 013 | 미생성 | ☐ |
| [05 AWS EKS Terraform Plan과 Preview](../../tickets/provisioning/05-aws-preview.md) | DRAFT | 01 | PROV-002, 003, 007, 014, 021 | 미생성 | ☐ |
| [06 GCP GKE Terraform Plan과 Preview](../../tickets/provisioning/06-gcp-preview.md) | DRAFT | 01 | PROV-002, 003, 008, 014, 021 | 미생성 | ☐ |
| [07 Plan Revision 승인과 Temporal 실행 차단](../../tickets/provisioning/07-approval-gate.md) | DRAFT | 01 | PROV-014, 015, 016, 021 | 미생성 | ☐ |
| [08 승인된 Plan으로 AWS EKS 기반 준비](../../tickets/provisioning/08-aws-infra-apply.md) | DRAFT | 05, 07 | PROV-007, 015, 016, 017 | 미생성 | ☐ |
| [09 AWS EKS에 DB·앱 배포 및 연결 확인](../../tickets/provisioning/09-aws-workloads.md) | DRAFT | 02, 03, 08 | PROV-010, 011, 012, 013, 018 | 미생성 | ☐ |
| [10 승인된 Plan으로 GCP GKE 기반 준비](../../tickets/provisioning/10-gcp-infra-apply.md) | DRAFT | 06, 07 | PROV-008, 015, 016, 017 | 미생성 | ☐ |
| [11 GCP GKE에 DB·앱 배포 및 연결 확인](../../tickets/provisioning/11-gcp-workloads.md) | DRAFT | 02, 03, 10 | PROV-010, 011, 012, 013, 018 | 미생성 | ☐ |
| [12 승인된 K3s Agent 운영 배포](../../tickets/provisioning/12-k3s-approved-rollout.md) | DRAFT | 04, 07 | PROV-009, 015, 016, 017, 018 | 미생성 | ☐ |
| [13 실제 검증 결과와 Migration 인계](../../tickets/provisioning/13-result-handoff.md) | DRAFT | 12 | PROV-018, 019, 020, 021 | 미생성 | ☐ |
| [14 실패 복구와 세 대상 통합 검증](../../tickets/provisioning/14-recovery-integration.md) | DRAFT | 08, 09, 10, 11, 12, 13 | PROV-015, 016, 017, 018, 019, 021 | 미생성 | ☐ |

## 선행 관계

```text
01 ─┬→ 02 → 03 → 04 ──────────────→ 12 → 13 ─┐
    ├→ 05 ────────→ 08 → 09 ──────────────────┤
    ├→ 06 ────────→ 10 → 11 ──────────────────┤→ 14
    └→ 07 ─┬──────→ 08, 10 ────────────────────┤
             └──────→ 12 ──────────────────────┘
```

화살표는 주요 경로를 요약한다. 정확한 모든 `Blocked by`는 위 표와 각 티켓에 있다. `05`·`06`·`07`은 `01` 이후 독립 진행할 수 있고, AWS/GCP의 배포 경로는 공통 Helm 기반과 해당 인프라 준비가 끝나면 독립 진행할 수 있다. `13`은 K3s 경로로 결과 계약을 먼저 검증한다. AWS/GCP 실제 경로와의 연결은 `14`에서 확인한다.

## 검토 및 결정 대기

- 사용자 검토에서 티켓의 크기·선행 관계·Test Seam 후보를 확정한다. 특히 `01`은 Analysis/Sandbox/Build의 실제 출력 계약과 Planning의 조합 책임을 먼저 맞춘다. 01의 내부 v1 구현과 공개 Planning/Adapter 테스트는 완료했다. 다른 팀의 실제 wire schema·참조 조회·실환경 연동은 후속 통합 범위이며, 내부 계약과의 변환 경계를 유지한다. 나머지 티켓은 검토 전 `DRAFT`다.
- 제품 API 경로·필드·승인 응답 보장과 기존 `ADR-001` 번호 충돌은 아직 미해결이다. 특히 07의 실제 HTTP 계약 구현 전에 결정한다.
- AWS/GCP 클러스터 생성 범위, Terraform Backend/Lock, Node·네트워크 규모, Registry/Secret/TLS, Agent 완료 통지, 실패 후 자동 정리, 실제 Cloud E2E 범위는 관련 티켓에서 결정 전제다.
- 티켓 이력은 유지한다. 요구사항이 바뀌면 영향받는 티켓과 SDD를 함께 갱신하고, 전체 티켓을 다시 생성하지 않는다.
