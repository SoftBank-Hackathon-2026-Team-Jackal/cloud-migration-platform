# Ticket 01 — AWS→온프레미스 데이터 이전 경로 Preflight

## 상태

TODO — [Issue #51](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/51); 설계·이슈 발행 완료, 구현 전

## 목적

원본 AWS PostgreSQL에서 온프레미스 K3s로 데이터 이전을 시작하기 전에 실제 경로의 CIDR·라우팅·DB 연결·대량 전송을 확인하고 필수 검사 실패 또는 확인 불가 시 시작을 차단한다.

## 선행 조건

- 원본 DB의 실제 사설 Endpoint/포트와 TLS·인증 참조, AWS와 온프레미스의 라우팅 대상 CIDR, 터널 경로가 제공되어야 한다.
- 복제 작업과 동등한 네트워크 위치에서 검사를 실행할 Agent/K3s 접근이 준비되어야 한다.
- Provisioning 13의 결과 인계가 없으면 동등한 검증용 입력으로 모듈을 만들되 상위 Workflow 연결 완료로 표시하지 않는다.

## 포함 범위

- [MNP-001~006](../../specs/migration-network-preflight/spec.md)의 요청·결과 계약, CIDR 충돌, 대상 라우트, DNS/TCP/TLS/DB 읽기, 기본 4 MiB 읽기 전용 전송, 선택적 MTU 진단, `PASS/FAIL/UNKNOWN` 판정
- 온프레미스 Agent의 허용 목록 기반 전용 검사 작업과 중복·응답 유실 시 결과 재조회
- Data Migration 시작 전 호출·차단 연결 및 민감정보 제거 결과 기록

## 제외 범위

WireGuard 구축, CIDR/NAT/라우트/방화벽/MTU 자동 수정, CDC/slot/publication/subscription 구성, 실행 중 지연·WAL 누적 감시, 앱 트래픽 경로·최종 기능 검증.

## 수용 기준

- [ ] 필수 검사 모두 통과한 실제 경로에서만 초기 복사·CDC 시작을 허용한다.
- [ ] CIDR 충돌, 라우트 부재, DB 접속 단계별 실패, 64 KiB 이후 전송 중단, Agent 접근 불가에서 원인·증거를 남기고 시작을 막는다.
- [ ] ICMP만 차단된 경로를 MTU 실패로 단정하지 않고, DB 읽기 이외의 데이터 변경을 수행하지 않는다.
- [ ] Secret 원문·전송 데이터 본문이 작업 결과·로그·Workflow History에 없다.
- [ ] 설정·Endpoint 변경 뒤 이전 `PASS` 결과를 재사용하지 않는다.

## 테스트 전략

순수 CIDR/판정 단위 테스트, 가짜 실행기의 연결·전송 실패 주입, 격리 K3s↔PostgreSQL 통합 검사와 Data Migration 시작 차단을 검증한다. 실제 AWS↔온프레미스 경로는 해당 환경에서 수행한 증거와 미검증 사유를 별도로 기록한다. 검사 성공은 CDC 지속성이나 WAL 안정성을 검증하지 못한다.

## 관련 문서

- [기능 명세](../../specs/migration-network-preflight/spec.md) · [구현 계획](../../specs/migration-network-preflight/plan.md) · [작업 목록](../../specs/migration-network-preflight/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
