# Tasks: Migration Network Preflight

2026-11-05 사용자 합의에 따른 AWS→온프레미스 K3s 데이터 이전 전 네트워크 검사 작업이다. 공통 모듈·K8s 실행기·Activity·시작 판정과 온프레미스 Agent 작업 경로(N3)를 구현하고 격리 k3d에서 검증했다. 실제 AWS↔온프레미스 경로 검증과 Data Migration Workflow 연결(데이터 담당)이 남아 있다.

| ID·티켓 | 상태 | 선행 조건 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- |
| [01 공통 검사 모듈과 Data Migration 시작 차단](../../tickets/migration-network-preflight/01-onprem-data-path.md) | IN_REVIEW | 원본 DB 사설 Endpoint·실제 복제 실행 위치·인증 참조 제공 | [#51](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/51) | ☐ |

검사 모듈의 코드는 공통 담당자가 구현한다. Data Migration 담당자는 시작 전 호출·차단과 CDC 중 지연/WAL 감시를 연결한다. 온프레미스 Agent의 전용 검사 실행 경로는 공통 모듈 구현 범위에 포함한다. Traffic Migration 담당자는 결과를 참고하고 사용자→앱 경로를 별도 검증한다.
