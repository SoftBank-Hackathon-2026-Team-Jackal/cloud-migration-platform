# Migration Network Preflight — 구현 계획

## 경계와 배치

`Data Migration Worker → run_network_preflight(request) → 온프레미스 Agent 전용 검사 작업 → NetworkPreflightResult`를 공개 경계로 둔다. Workflow는 단계 전환과 결과 참조만 다루고 네트워크·DB I/O는 Activity/Agent에서 수행한다. 기존 `RolloutRunner.preflight`는 클러스터/스토리지/Secret 준비 검사이므로 의미와 작업 유형을 분리한다. Agent에 임의 셸 명령 실행 권한을 추가하지 않고 허용 목록의 검사만 수행한다.

## 구현 단위

1. 공통 요청·결과 모델에 실제 DB Endpoint, Secret 참조, 라우팅 대역, 실행 위치, 제한 시간·최대 바이트를 정의한다. IPv4 P0 범위와 필수 입력·오류 코드를 명시한다.
2. CIDR 검사는 순수 함수로 구현한다. 실제 라우팅 대상만 비교하고 VPC 내부 Subnet의 정상 포함 관계는 허용한다.
3. 현장 검사 실행기는 복제 작업과 동등한 네트워크 위치에서 DNS/라우트/TCP/TLS/인증/읽기 전용 전송을 수행한다. Pod의 첫 홉과 터널 게이트웨이의 실제 경로를 대조해 공개 인터넷 우회를 통과로 표시하지 않는다. Agent 작업 ID를 `migration_id`와 검사 입력 버전에 묶어 중복 실행·응답 유실 뒤 결과 재조회를 지원한다. 검사 환경 생성/정리는 실행기 범위에 둔다.
4. MTU/DF 진단은 권한·ICMP 허용 시 시도하는 보조 증거다. 큰 DB 응답의 완료/중단 여부가 실제 경로 검사 근거다.
5. Data Migration Activity는 전체 `PASS`가 아니면 초기 복사·CDC를 시작하지 않고 결과·실패 단계를 저장한다. 터널·라우트·방화벽·Endpoint 변경 뒤 재시작은 새 검사 입력 버전으로 실행한다.

## 검증 경계

- 순수 CIDR 및 결과 판정은 단위 테스트로 검증한다.
- 가짜 Agent/DB 실행기로 DNS·라우트·TCP·TLS·인증·전송 중단·검사 불가·Secret 정제를 확인한다.
- 격리 K3s와 PostgreSQL에서 실제 검사 위치·읽기 전용 전송·차단을 통합 검증한다. 실제 AWS↔온프레미스 WireGuard 경로의 성공은 해당 환경에서 별도 증거가 있어야만 주장한다.
- 검사 `PASS`는 복제 프로토콜, publication/subscription/slot, CDC 중 WAL 누적과 Cutover 준비를 보증하지 않는다. Data Migration 담당자가 별도로 확인한다.

## 후속 연결

Provisioning 결과의 네트워크·Endpoint 참조 연결은 [Provisioning 13](../../tickets/provisioning/13-result-handoff.md)과 맞춘다. Traffic Migration은 결과의 경로·시각을 읽을 수 있지만 사용자→앱 네트워크 검사는 별도다. WireGuard 구축/운영 주체, 원본 DB의 실제 사설 Endpoint와 인증 방식은 구현 시 Data Migration 담당자와 입력으로 맞춘다.
