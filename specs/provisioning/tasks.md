# Tasks: Provisioning

이 파일은 7단계 Provisioning 티켓 **초안 인덱스**다. 각 티켓의 상세 수용 기준·Test Seam·검증 방법은 연결된 파일에 있다. 01은 사용자 요청(2026-11-03, #5)에 따라 입력 계약·로컬 검증·대상별 구성 예시와 회귀 검증을 완료했다. CP-01은 PR #10으로 main에 반영됐다. 02는 PR #12로 main에 반영됐다. 03은 PR #14로 main에 반영됐다. 04는 사용자 승인 후 실제 격리 K3s 검증을 완료했다. B-01(#17)의 공통 빌드 모듈은 PR #18로 main에 반영됐고 Workflow/Worker 연결은 후속 통합 범위다. 05/06/07은 `TODO`이며 한 티켓씩 순차 진행한다. 나머지 `DRAFT`는 사용자 검토 전 실제 구현을 시작하지 않는다. 상태는 `DRAFT → TODO → IN_PROGRESS → DONE`을 기본으로 하고, 선행 작업이나 결정이 막히면 `BLOCKED`로 기록한다. 검토 후 구현 전 GitHub Issue를 생성하거나 기존 Issue를 연결하고 아래 번호를 기록한다. `DONE`은 [개발 워크플로](../../docs/engineering/development-workflow.md)의 문서·테스트·리뷰 조건을 모두 충족한 경우에만 사용한다.

| ID·티켓 | 상태 | Blocked by | 관련 요구사항 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [01 공통 입력 검증과 대상별 구성 예시](../../tickets/provisioning/01-provider-spec-validation.md) | DONE | 없음 | PROV-001, 002, 003 | [#5](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/5) | ☑ |
| [P0-01 정책 정렬·병렬 착수 조건](../../tickets/provisioning/p0-01-policy-alignment.md) | DONE | 01 | PROV-001, 014, 015, 021 | [#7](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/7) | ☑ |
| [CP-01 최소 공유 계약](../../tickets/provisioning/cp-01-shared-contracts.md) | DONE | 01, P0-01 main 반영 | PROV-005, 006, 014, 015, 021 | [#9](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/9) | ☑ |
| [02 PostgreSQL Chart 렌더링과 PVC 검사](../../tickets/provisioning/02-postgres-helm-render.md) | DONE | 01, CP-01 | PROV-005, 006, 011 | [#11](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/11) | ☑ |
| [03 앱 배포와 .env·Secret 바인딩 렌더링](../../tickets/provisioning/03-app-env-render.md) | DONE | 01, 02 | PROV-004, 005, 010, 012, 021 | [#13](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/13) | ☑ |
| [04 격리 K3s에서 Agent 배포 경로 검증](../../tickets/provisioning/04-k3s-agent-test-deploy.md) | DONE | 02, 03 | PROV-006, 009, 011, 013 | [#15](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/15) | ☑ |
| [B-01 S3 소스 공통 빌드와 ECR Digest 반환](../../tickets/provisioning/b-01-image-build.md) | DONE | ADR-003 | PROV-001, 004, 010, 021 | [#17](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/17) | ☑ |
| [05 AWS EKS Terraform Plan과 Preview](../../tickets/provisioning/05-aws-preview.md) | TODO | 01, CP-01 | PROV-002, 003, 007, 014, 021 | 미생성 | ☐ |
| [06 GCP GKE Terraform Plan과 Preview](../../tickets/provisioning/06-gcp-preview.md) | TODO | 01, CP-01 | PROV-002, 003, 008, 014, 021 | 미생성 | ☐ |
| [07 Plan Revision 승인과 Temporal 실행 차단](../../tickets/provisioning/07-approval-gate.md) | TODO | 01, CP-01 | PROV-014, 015, 016, 021 | 미생성 | ☐ |
| [08 승인된 Plan으로 AWS EKS 기반 준비](../../tickets/provisioning/08-aws-infra-apply.md) | DRAFT | 05, 07 | PROV-007, 015, 016, 017 | 미생성 | ☐ |
| [09 AWS EKS에 DB·앱 배포 및 연결 확인](../../tickets/provisioning/09-aws-workloads.md) | DRAFT | 02, 03, 08 | PROV-010, 011, 012, 013, 018 | 미생성 | ☐ |
| [10 승인된 Plan으로 GCP GKE 기반 준비](../../tickets/provisioning/10-gcp-infra-apply.md) | DRAFT | 06, 07 | PROV-008, 015, 016, 017 | 미생성 | ☐ |
| [11 GCP GKE에 DB·앱 배포 및 연결 확인](../../tickets/provisioning/11-gcp-workloads.md) | DRAFT | 02, 03, 10 | PROV-010, 011, 012, 013, 018 | 미생성 | ☐ |
| [12 승인된 K3s Agent 운영 배포](../../tickets/provisioning/12-k3s-approved-rollout.md) | DRAFT | 04, 07 | PROV-009, 015, 016, 017, 018 | 미생성 | ☐ |
| [13 실제 검증 결과와 Migration 인계](../../tickets/provisioning/13-result-handoff.md) | DRAFT | 12 | PROV-018, 019, 020, 021 | 미생성 | ☐ |
| [14 실패 복구와 세 대상 통합 검증](../../tickets/provisioning/14-recovery-integration.md) | DRAFT | 08, 09, 10, 11, 12, 13 | PROV-015, 016, 017, 018, 019, 021 | 미생성 | ☐ |

## 선행 관계

```text
01 → P0-01 → CP-01
                ├→ 02 → 03 → 04 → 12 → 13
                ├→ 05 → 08 → 09
                ├→ 06 → 10 → 11
                └→ 07 → 08 / 10 / 12
ADR-003 → B-01 → Sandbox/Planning 동일 이미지 검증 → 09 / 11 / 12
08, 09, 10, 11, 12, 13 → 14
```

화살표는 주요 경로를 요약한다. 정확한 모든 `Blocked by`는 위 표와 각 티켓에 있다. `02`·`05`·`06`·`07`은 `CP-01`이 main에 반영된 뒤 의존성상 독립이지만 현재는 순차로 진행하며, AWS/GCP의 배포 경로는 공통 Helm 기반과 해당 인프라 준비가 끝나면 독립 진행할 수 있다. `13`은 K3s 경로로 결과 계약을 먼저 검증한다. AWS/GCP 실제 경로와의 연결은 `14`에서 확인한다.

## 검토 및 결정 대기

- 사용자 검토에서 티켓의 크기·선행 관계·Test Seam 후보를 확정한다. 특히 `01`은 Analysis/Sandbox/Build의 실제 출력 계약과 Planning의 조합 책임을 먼저 맞춘다. 01의 내부 v1 구현과 공개 Planning/Adapter 테스트는 완료했다. 다른 팀의 실제 wire schema·참조 조회·실환경 연동은 후속 통합 범위이며, 내부 계약과의 변환 경계를 유지한다. 02/03은 렌더, 04는 로컬 폴링 Agent와 실제 격리 K3s 배포 검증 완료이며 05/06/07은 범위 검토 후 TODO다. 티켓별 착수 조건을 충족해야 한다. 나머지는 검토 전 DRAFT다.
- Preview/승인 유지, DB 승인 기록+Signal, 범용 Outbox 보류는 ADR-002로 결정했다. 07의 실제 API URL·필드·인증/권한·응답 계약은 담당자와 합의한다. ADR-002는 P0 범위 결정이며 ADR-001 스캐폴드 결정과 충돌하지 않는다.
- AWS/GCP 기존 네트워크 재사용 P0 제외와 중앙 S3 State+잠금은 결정했다. Node·신규 네트워크 Profile, Backend 실제 설정, 검증 계정/권한, Registry/Secret/TLS, Agent 완료 통지, 실패 후 정리는 관련 티켓의 결정 전제다.
- 티켓 이력은 유지한다. 요구사항이 바뀌면 영향받는 티켓과 SDD를 함께 갱신하고, 전체 티켓을 다시 생성하지 않는다.

## 현재 진행 방식

사용자 지시에 따라 CP-01부터 한 티켓씩 순차 진행한다. 병렬 채팅을 만들거나 병렬 준비용 작업을 별도 수행하지 않는다. 문서는 관련 구현 이슈/PR에 함께 반영한다. 실제 공유 인터페이스는 [shared-contracts.md](shared-contracts.md)를 따른다.

## 이미지 흐름 변경 (ADR-003, Issue #17)

이전 내부 v1 Planning은 Build가 Sandbox를 참조하는 순서로 구현돼 있다. 새 제품 정책은 Build 선행·Sandbox 실행 Digest 검증·동일 Digest 배포다. [B-01](../../tickets/provisioning/b-01-image-build.md)은 공통 빌드 모듈로 범위를 좁혔다. 후속 통합 작업에서 코드/fixture를 전환하기 전까지 현재 01의 DONE은 **당시 범위의 완료**이고 새 이미지 동일성 정책의 구현 증거가 아니다. 04의 격리 Agent 배포 테스트도 수정 루프/Build 모듈의 완료 증거가 아니다.
