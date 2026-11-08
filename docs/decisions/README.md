# 설계 결정 기록(ADR)

중요한 기술 선택을 한 결정마다 파일 하나로 남깁니다. 새 결정은 다음 번호로 추가하고, 기존 ADR을 바꾸는 결정이면 새 ADR에 "바꾸는 문서"를 적습니다. 기존 ADR의 본문은 조용히 고쳐 쓰지 않습니다([개발 절차](../engineering/development-workflow.md)).

| 번호 | 결정 | 상태 | 영역 |
| --- | --- | --- | --- |
| [ADR-001](./ADR-001.md) | Temporal을 오케스트레이션 스캐폴드로 쓴다 | Accepted(초기 스캐폴드) | 공통 |
| [ADR-002](./ADR-002.md) | P0 Provisioning 범위와 병렬 작업 경계 | Accepted | Provisioning |
| [ADR-003](./ADR-003.md) | Sandbox에서 검증한 이미지 Digest를 Production에서 재사용한다 | Accepted | Provisioning·Sandbox |
| [ADR-004](./ADR-004.md) | Claude 연결과 비용 상한 | Accepted | 분석·검증 |
| [ADR-005](./ADR-005.md) | 원본·대상 환경 연결 방식(AWS 역할 위임, GCP 고정 프로젝트) | Accepted | Provisioning |
| [ADR-006](./ADR-006.md) | 트래픽 전환은 Envoy 중계로 한다 | Accepted | 트래픽 |
| [ADR-007](./ADR-007.md) | 온프레미스 데이터 이전 경로는 원본 쪽 게이트웨이 입구로 한다 | Accepted | 데이터 이전 |
| [ADR-008](./ADR-008.md) | Sandbox 레드팀(능동 공격) 검증 | Proposed | 분석·검증 |
| [ADR-009](./ADR-009.md) | 플랫폼 스택은 FastAPI API와 Temporal Worker로 한다 | Accepted(구현 기록) | 공통 |
| [ADR-010](./ADR-010.md) | 데이터 이전은 PostgreSQL 논리 복제로 하고, 전환은 원본 쪽 PgBouncer로 한다 | Accepted(구현 기록) | 데이터 이전 |
| [ADR-011](./ADR-011.md) | Worker는 `kubectl exec`로 SQL을 실행하고, EKS 토큰은 Activity 사이에서 재사용한다 | Accepted(구현 기록) | 데이터 이전 |
| [ADR-012](./ADR-012.md) | 전환할 때 원본 앱의 NetworkPolicy에는 허용 규칙만 더한다 | Accepted(구현 기록) | 데이터 이전 |
| [ADR-013](./ADR-013.md) | DB 전환 승인 전에 그림자 리허설로 대상 응답을 관찰한다 | Accepted(구현 기록) | 트래픽·데이터 이전 |
| [ADR-014](./ADR-014.md) | 시연 플랫폼은 단일 EC2의 Docker Compose로 운영하고, main 커밋만 배포한다 | Accepted(구현 기록) | 시연 인프라 |

"구현 기록"은 이미 구현되어 동작하는 결정을 2026-11-08에 문서로 옮겼다는 뜻입니다. 근거가 된 PR은 각 ADR의 Status 줄에 있습니다. 당시 대안을 비교한 기록이 없는 부분은 ADR 본문에 그렇게 밝혀 두었습니다.
