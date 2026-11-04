# Ticket 01 — AWS→온프레미스 데이터 이전 경로 Preflight

## 상태

IN_REVIEW — [Issue #51](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/51); 착수 결정 N1~N8. 공통 모듈·K8s 실행기·Activity·시작 판정 구현과 격리 k3d 실검증 완료. Agent 작업 경로 연결(Provisioning 15 이후)과 실제 AWS↔온프레미스 경로 검증이 남아 DONE이 아니다.

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

## 착수 결정 (2026-11-05)

사용자 지시: 기획 문서의 최신 결정을 우선 적용하고 없는 항목은 구현자가 최선안을 정해 기록한다. 다른 세션이 Provisioning 15(Agent 상시 실행, `onprem_agent`·`remote_agent`)를 작업 중이므로 같은 파일은 건드리지 않는다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| N1 배치 | 계약 `cmp_contracts.network_preflight`(`NetworkPreflightRequest`·`NetworkPreflightResult`·`PreflightCheck`). 순수 판정·조립 `cmp_providers.network_preflight`(`check_cidr_overlap`, `input_fingerprint`, `run_network_preflight(request, executor)`, 시작 판정 `may_start_data_migration(result, request)`). 현장 실행기 `cmp_providers.network_preflight_k8s.KubernetesProbeExecutor`(명시적 kubeconfig/context). Worker Activity `run_network_preflight`(`migration-activities` queue). | plan "공개 진입점 한 번 호출". 기존 Provider 패키지 구성과 같다. |
| N2 실행 위치·허용 목록 | 검사는 복제 작업이 실행될 Namespace의 일반 Pod 네트워크에서 고정 스크립트 하나로만 실행한다(요청 값은 검증 후 환경변수로만 전달, 임의 명령 없음). 이미지는 Digest 고정이며 Worker/Agent 설정의 허용 목록에 있어야 한다. 노드 라우팅(터널 경유)은 같은 노드에 고정한 읽기 전용 `hostNetwork` Pod의 `ip route get`으로 확인한다. `hostNetwork` Pod를 만들 수 없으면 라우트는 `UNKNOWN`. | MNP-003 "실행 위치와 터널 게이트웨이에서 선택되는 경로". Pod 안 첫 홉은 항상 CNI라 터널 확인 근거가 못 된다. |
| N3 Agent 경로 | 실행기는 Agent가 가진 kubeconfig로 동작하도록 만들고, Agent 작업 유형 연결(작업 큐·재조회)은 15 병합 후 별도 작은 변경으로 한다. 그 전에는 실행기를 직접 호출해 검증한다. Kubernetes 접근 불가는 `UNKNOWN`/`PROBE_LOCATION_UNAVAILABLE`. | 15와 같은 파일 충돌 방지. 미연결은 완료 조건의 미검증으로 기록 |
| N4 재사용 금지 | 결과에 요청 Fingerprint(경로·Endpoint·TLS·CIDR·실행 위치·전송량, 인증 참조 이름 포함 canonical JSON의 SHA-256)를 둔다. 시작 판정은 전체 `PASS`이면서 현재 요청 Fingerprint와 같을 때만 허용한다. | MNP-006 "변경 후 이전 PASS 재사용 금지" |
| N5 판정 코드 | CIDR: `CIDR_OVERLAP`(FAIL), `CIDR_INPUT_MISSING`(UNKNOWN). 라우트: `NO_ROUTE`·`DESTINATION_NOT_ALLOWED`·`UNEXPECTED_PATH`(FAIL), `ROUTE_UNVERIFIED`(UNKNOWN). DB: `DNS_FAILED`·`TCP_UNREACHABLE`·`TLS_FAILED`·`AUTH_FAILED`·`QUERY_FAILED`(FAIL). 전송: `TRANSFER_INTERRUPTED`(수신량·64 KiB 초과 여부 기록)·`TRANSFER_TIMEOUT`·`TRANSFER_SHORT`(FAIL). 실행 위치 접근 불가 `PROBE_LOCATION_UNAVAILABLE`(UNKNOWN). MTU는 보조 증거로만 남기며 판정에 쓰지 않는다. | MNP-002~006, 방화벽 규칙 이름은 만들지 않음 |
| N6 상한 | 전송량 기본 4 MiB·최대 64 MiB, 제한 시간 기본 60초·최대 600초, 검사 내부 재시도 없음. 같은 요청의 재실행은 안전(읽기 전용, 검사 Pod는 실행마다 새 이름으로 만들고 끝나면 삭제). | MNP-005 "유한한 상한" |
| N7 비밀 | DB 인증은 대상 Namespace Secret의 이름·키 참조만 받는다. Pod에 `secretKeyRef`로만 주입하고 결과·로그·History에는 남기지 않는다. 결과 증거는 코드·IP·인터페이스·바이트 수·시간만. | MNP-001·수용 기준 3 |
| N8 Test Seam | `run_network_preflight(request, executor)` + 가짜 실행기(관측값 주입)로 판정·차단·재사용 금지를 확인한다. K8s 실행기는 기록용 kubectl 러너로 매니페스트(Secret 참조·허용 이미지·명령 고정)를 검사한다. 통합: 격리 k3d에 원본 역할 PostgreSQL을 두고 정상·인증 실패·NetworkPolicy 차단(TCP)·대량 전송을 실제로 실행한다. 64 KiB 이후 끊김·비대칭 경로는 가짜 실행기로만 확인한다. Data Migration Workflow 연결은 Data Migration 담당 범위이므로 Activity와 시작 판정 함수까지 제공한다. | tasks.md "Data Migration 담당자가 시작 전 호출·차단 연결" |

## 구현 (2026-11-05)

- 계약 `cmp_contracts.network_preflight`: 요청(경로·Endpoint·TLS·Secret 참조·CIDR·기대 터널·실행 위치·상한), 실행기 관측값 `ProbeObservation`, 결과(검사별 상태·코드·작은 증거, 검사 시각·Endpoint·실행 위치·입력 Fingerprint). IPv4 CIDR은 정규화해 받는다.
- 판정 `cmp_providers.network_preflight`: `check_cidr_overlap`(AWS↔온프레미스·터널 사이만 비교, 같은 역할 안의 포함 관계는 정상), `run_network_preflight`(CIDR 실패 시 현장 검사 생략, 실행 위치 접근 불가는 `UNKNOWN`, 필수 4개 검사가 모두 PASS일 때만 PASS), `may_start_data_migration`(PASS이면서 현재 요청 Fingerprint와 같을 때만 허용).
- 실행기 `cmp_providers.network_preflight_k8s.KubernetesProbeExecutor`: 허용 목록·Digest 고정 이미지의 고정 스크립트 Pod 한 개(일반 Pod 네트워크, 비루트·읽기 전용 루트·권한 제거, 인증은 `secretKeyRef`) → DNS·TCP·TLS·인증·`SELECT 1`·`repeat()` 읽기 전송(제한 시간 내 수신 바이트 계산). 같은 노드에 고정한 읽기 전용 `hostNetwork` Pod가 `ip route get`으로 노드 경로(장치·게이트웨이·출발지)를 확인한다. 생성이 거부되면 라우트는 `UNKNOWN`. Pod는 실행마다 새 이름이며 끝나면 삭제한다.
- Worker: Activity `run_network_preflight`(`migration-activities` queue, Heartbeat). 설정(`PREFLIGHT_KUBECONFIG`·`PREFLIGHT_CONTEXT`·`PREFLIGHT_PROBE_IMAGE`·`PREFLIGHT_ALLOWED_IMAGES`)이 없으면 `UNKNOWN`/`PROBE_LOCATION_UNAVAILABLE`로 끝나 PASS가 되지 않는다.
- MTU/DF 보조 진단은 기본 이미지(busybox `ping`에 DF 옵션 없음)에서 실행하지 않고 `NOT_ATTEMPTED`로 남긴다. 판정에는 쓰지 않는다(수용 기준 3과 일치).

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_network_preflight.py tests/test_network_preflight_k8s.py` | 39개 통과. CIDR 충돌·입력 누락, 라우트 4종(미확인·없음·예상 밖 경로·허용 밖 목적지), 공개 인터넷 우회 FAIL, DB 단계 5종 구분, 64 KiB 이후 끊김·짧은 수신·시간 초과, ICMP만 막힘은 통과, 실행 위치 불가, CIDR 실패 시 현장 검사 생략, 경로·Endpoint·TLS·터널·위치 변경 시 이전 PASS 재사용 거절, 결과에 비밀값 없음, 상한, K8s 매니페스트(고정 스크립트·`secretKeyRef`·허용 이미지·`hostNetwork` 라우트 Pod·정리), Activity·미설정 Worker |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff·전체 634개 통과 |
| `make smoke-temporal` (로컬 compose Temporal, 2026-11-05) | 저장소 스크립트 그대로는 `MigrationWorkflow`가 `sandbox` 단계에서 멈춰 실패했다. 분석 #45가 Sandbox child Workflow를 추가했지만 스크립트에 Sandbox Worker가 빠져 있기 때문이다(main에 원래 있던 문제, 분석 담당 범위라 수정하지 않음). Sandbox Worker만 추가한 같은 스크립트로 실행해 `scaffold_complete`를 확인했다. Provisioning Worker(`get_migration_handoff` 포함)와 Migration Activity Worker(`run_network_preflight` 포함) 등록에 문제가 없다. |
| 격리 k3d `cmp-preflight`(K3s v1.36.4, ARM64) + 원본 역할 PostgreSQL 16, 실제 `KubernetesProbeExecutor` | 정상 4 MiB PASS(0.08초)·32 MiB PASS(0.92초), 시작 판정 허용 → 포트 변경 시 `PREFLIGHT_INPUT_CHANGED`로 거절. 잘못된 비밀번호 `AUTH_FAILED`, TLS 필수인데 서버 미지원 `TLS_FAILED`, 없는 이름 `DNS_FAILED`(라우트 `ROUTE_UNVERIFIED`), NetworkPolicy 수신 차단 `TCP_UNREACHABLE`, 기대 터널 `wg0`인데 실제 `eth0` → `UNEXPECTED_PATH`, CIDR 중복 → 현장 검사 생략. 검사 Pod 잔존 0, 결과에 비밀번호 없음. 증거: [mnp-01-k3d.json](../../docs/verification/mnp-01-k3d.json). 정리: k3d 클러스터 삭제, 기본 kubeconfig 해시 동일 |

### 미검증·남은 작업

- **Agent 작업 경로(N3):** 실행기를 온프레미스 Agent의 허용 작업 유형으로 연결하고(작업 ID를 `migration_id`·입력 Fingerprint에 묶어 중복 실행·응답 유실 뒤 재조회) Agent가 실행하도록 하는 부분은 Provisioning 15(같은 Agent 파일 작업 중)가 병합된 뒤 진행한다. 현재는 Worker가 명시적 kubeconfig로 실행기를 직접 호출한다.
- **실제 AWS↔온프레미스 경로:** WireGuard·원본 DB 사설 Endpoint가 아직 없어 실제 경로에서 검사하지 않았다. k3d에서는 Service CIDR을 원본 쪽으로 대신 썼고 터널 인터페이스 확인은 "예상 밖 경로" 실패만 확인했다.
- 64 KiB 이후 끊김·비대칭 경로는 가짜 실행기로만 확인했다. `verify-full` TLS(`sslrootcert=system`)는 실제 인증서 서버에서 확인하지 않았다.
- Data Migration Workflow에서 시작 전에 Activity를 호출하고 `may_start_data_migration`으로 막는 연결은 Data Migration 담당 범위다(tasks.md). 공통 모듈은 Activity와 판정 함수까지 제공한다.
- 검사 성공은 복제 권한·publication/subscription/slot·CDC 중 WAL 안정성을 보증하지 않는다.

