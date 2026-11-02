# Provisioning 병렬 작업과 병합 규칙 — 비활성 이력

**2026-11-03 사용자 후속 지시로 현재는 한 티켓씩 순차 진행한다.** 아래 내용은 이전 검토 이력이며 새 채팅 생성·병렬 작업 준비를 요구하지 않는다. 현재 지침은 AGENTS.md와 development-workflow.md이며 공유 코드 인터페이스는 [shared-contracts.md](shared-contracts.md)에 있다.

## 현재 판단 (2026-11-03)

01은 PR #6으로 main에 반영됐다. [ADR-002](../../docs/decisions/ADR-002.md)의 P0 결정은 합의됐으나 [CP-01](../../tickets/provisioning/cp-01-shared-contracts.md)의 공통 코드가 아직 없다. **지금 새 채팅 네 개에서 02/05/06/07 구현을 동시에 시작하지 않는다.** 먼저 CP-01을 구현·검증·병합한다. 새 채팅을 만드는 것 자체는 병합 충돌을 해결하지 않는다.

## 착수 체크리스트

- [ ] 정책 정렬 #7과 CP-01의 코드·테스트가 main에 반영되고 기준 commit SHA를 기록했다.
- [ ] 각 티켓의 Issue, 수용 기준, Test Seam과 담당 파일이 지정됐다.
- [ ] 각 채팅은 서로 다른 Git worktree와 브랜치를 사용하며 같은 기준 SHA에서 시작한다. 같은 로컬 작업 디렉터리에서 동시에 수정하지 않는다.
- [ ] 05/06은 신규 네트워크 Profile·실제 Plan 검증 계정/프로젝트·자격 증명 참조를 확보했다. 미확보 시 로컬 구현과 실제 검증의 완료 상태를 구분한다.
- [ ] 07의 승인 URL/인증·권한/응답 계약을 담당자와 합의했다. 인증 주체를 요청 JSON에서 신뢰하는 임시 API를 만들지 않는다.

## 소유 범위

아래 파일은 기존 파일뿐 아니라 새로 만들 위치에 대한 예약이다. 제안 경로가 바뀌면 분기 전 갱신한다.

| 담당 | 주된 변경 영역 | 단독 수정하지 않을 영역 |
| --- | --- | --- |
| 공통/통합 | `cmp_contracts/plan.py`, `chart.py`, 공통 Digest·fixture, 기존 공통 `provisioning.py`, spec/plan/tasks, pyproject/lockfile | 각 Provider 구현 |
| 02 | `infra/helm/postgresql/`, `cmp_providers/postgres_render.py`, `tests/test_postgres_render.py`, 자기 티켓 | 앱 Chart, 승인 API, AWS/GCP Terraform, 공유 계약 |
| 05 | `infra/terraform/aws/`, `cmp_providers/aws_plan.py`, `cmp_worker/aws_preview.py`, `tests/test_aws_preview.py`, 자기 티켓 | GCP/Chart/API, 공통 Preview·Digest 모델 |
| 06 | `infra/terraform/gcp/`, `cmp_providers/gcp_plan.py`, `cmp_worker/gcp_preview.py`, `tests/test_gcp_preview.py`, 자기 티켓 | AWS/Chart/API, 공통 Preview·Digest 모델 |
| 07 | 승인 API·업무 DB/migration·Temporal 승인 흐름, 승인 테스트, OpenAPI, 자기 티켓 | Terraform/Chart, 공통 Preview·Digest 모델 |

`cmp_*`는 각각 기존 packages/apps 아래 해당 Python 패키지를 뜻한다. 하위 패키지를 새로 만들면 setuptools 등록이 필요한지 공통 담당자가 확인한다. 테스트 fixture는 `postgres/`, `aws/`, `gcp/`, `approval/`로 분리한다.

공통 계약 변경이 필요하면 각자가 임의 수정하지 않고 공통 담당자에게 구체적 변경을 전달한다. 공통 패치를 먼저 통합한 뒤 해당 채팅들이 갱신한다. 각 작업자는 자기 티켓에 검증·설계 차이를 기록하고, 공통 SDD/index 변경은 통합 담당자가 PR 완료 전에 반영한다. 이 규칙은 SDD 갱신 의무를 생략하는 예외가 아니다.

## 병합 순서와 검사

1. CP-01을 먼저 main에 반영한다. 네 작업은 완료된 공통 계약을 소비한다.
2. 02/05/06은 준비된 순서대로 **한 PR씩** 병합한다. 07의 공통 계약 소비 코드는 독립 개발할 수 있으나 최종 통합에서는 생성된 Preview·산출물로 승인 검사를 재검증한다.
3. 각 PR은 최신 main을 반영한 후 자기 티켓 검사와 `make check`를 통과해야 한다. 텍스트 충돌이 없더라도 동작 계약의 호환성을 확인한다.
4. 02는 Helm 산출물, 05/06은 실제 저장 Plan, 07은 DB/Temporal 승인 실패·중복·재전달 증거를 남긴다. 클라우드 Apply는 이 네 티켓의 완료 검사에 포함하지 않는다.
5. 최종 조합에서 Plan/Preview/Digest와 승인 기록 일치를 확인한다. 03의 앱 Chart가 아직 없으면 전체 앱 배포 계획의 완료/승인을 주장하지 않는다.

worktree는 편집 충돌을 격리할 뿐 논리적 병합 충돌을 없애지 않는다. 공통 계약 수정이 반복되거나 서로의 코드가 완료 조건이면 해당 부분은 직렬화한다.

## 새 채팅에 전달할 내용

티켓 ID·Issue, 기준 commit SHA, 단독 소유 파일, 읽기 전용 공통 계약, 제외 범위, 테스트 명령/환경, 미정 사항, 커밋 메시지의 Issue 번호, 최종 PR과 통합 담당자를 전달한다. 이 문서는 새 채팅 생성 명령이 아니며 실제 채팅은 사용자 요청 후 생성한다.
