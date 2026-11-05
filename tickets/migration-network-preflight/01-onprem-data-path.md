# Ticket 01 — AWS→온프레미스 데이터 이전 경로 Preflight

## 상태

IN_REVIEW — [Issue #51](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/51); 착수 결정 N1~N8. 공통 모듈·K8s 실행기·Activity·시작 판정 구현과 격리 k3d 실검증 완료. N3 Agent 작업 경로도 구현·격리 k3d 실검증 완료(아래 §N3). 실제 AWS↔온프레미스 경로 검증과 Agent 새 릴리스(승인 대기)가 남아 DONE이 아니다.

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
| N3 Agent 경로 | 실행기는 Agent가 가진 kubeconfig로 동작하도록 만들고, Agent 작업 유형 연결(작업 큐·재조회)은 15 병합 후 별도 작은 변경으로 한다. 그 전에는 실행기를 직접 호출해 검증한다. Kubernetes 접근 불가는 `UNKNOWN`/`PROBE_LOCATION_UNAVAILABLE`. **2026-11-05 연결 완료 — §N3 결정 N3a~N3h.** | 15와 같은 파일 충돌 방지 |
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

- **Agent 작업 경로(N3):** 구현·격리 k3d 검증 완료(§N3). 이 작업 유형이 든 Agent 이미지 릴리스(`agent-v0.2.0` 다음)는 사용자 승인 전이라 태그하지 않았다. 그 전 설치된 Agent는 `AGENT_JOB_UNSUPPORTED`(UNKNOWN)로 시작을 막는다.
- **실제 AWS↔온프레미스 경로:** WireGuard·원본 DB 사설 Endpoint가 아직 없어 실제 경로에서 검사하지 않았다. k3d에서는 Service CIDR을 원본 쪽으로 대신 썼고 터널 인터페이스 확인은 "예상 밖 경로" 실패만 확인했다.
- 64 KiB 이후 끊김·비대칭 경로는 가짜 실행기로만 확인했다. `verify-full` TLS(`sslrootcert=system`)는 실제 인증서 서버에서 확인하지 않았다.
- Data Migration Workflow에서 시작 전에 Activity를 호출하고 `may_start_data_migration`으로 막는 연결은 Data Migration 담당 범위다(tasks.md). 공통 모듈은 Activity와 판정 함수까지 제공한다.
- 검사 성공은 복제 권한·publication/subscription/slot·CDC 중 WAL 안정성을 보증하지 않는다.


## N3 Agent 작업 경로 (2026-11-05)

노트북 K3s는 인바운드 포트를 열지 않아 데모 서버 Worker가 kube API에 닿지 못한다. 그래서 온프레미스 검사는 클러스터 안 Agent가 실행한다. Provisioning 15·16·17·23·24가 병합된 `origin/main` 위에서 작업했다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| N3a 작업 유형 | Agent 작업 `NETWORK_PREFLIGHT`(`cmp_contracts.network_preflight.NetworkPreflightJob`: `mode`·`job_id`·`agent_id`·`cluster_uid`·검증된 `NetworkPreflightRequest`). DB 인증은 Secret 이름·키 참조뿐이다. 작업의 `agent_id`·`cluster_uid`는 `probe_target`과 같아야 계약이 성립한다. | 지시 "references only" |
| N3b 결과 = 관측값 | Agent는 `ProbeObservation`(코드·IP·인터페이스·바이트·시간)만 보고하고, 판정(`NetworkPreflightResult`)은 Worker가 같은 순수 함수 `run_network_preflight`로 만든다. | 지시는 "Result를 보고"였지만, Agent가 PASS를 스스로 정하지 않고 판정 규칙이 바뀌어도 Agent 재배포가 필요 없다. CIDR 실패 시 작업 자체를 만들지 않는다. 결과에 담기는 값은 같다(비밀 없음) |
| N3c 중복·재조회 | 작업 ID = `preflight-` + SHA-256(`migration_id`:입력 Fingerprint) 앞 24자. 같은 요청은 QUEUED/RUNNING 작업을 재사용한다. 끝난 작업의 관측값은 보고 후 300초(`REUSE_SECONDS`) 안에는 다시 돌려준다(응답 유실·Activity 재시도). 그 뒤 같은 요청은 같은 작업을 다시 QUEUED로 돌려 재검사한다. 같은 보고의 재전송은 수락한다. | Fingerprint는 Secret *값*을 담지 않으므로, 비밀번호를 고친 뒤 영원히 옛 FAIL을 돌려주지 않게 시간 창을 둔다. 새 열·마이그레이션 없이 결과의 `observed_at`(서버 시각)만 쓴다 |
| N3d 수신 제한 | `probe_target`의 등록 Agent만 큐에 넣는다(미등록 `AGENT_NOT_REGISTERED`, 폐기 `AGENT_REVOKED`, 클러스터 불일치 `AGENT_TARGET_MISMATCH`). 수신 시 다시 확인해 맞지 않으면 실행 전 `FAILED AGENT_TARGET_MISMATCH`. 다른 Agent는 수신·보고할 수 없다(기존 소유 범위 그대로). Agent 쪽도 작업 ID·Agent ID·실제 kube-system UID를 대조한다. | 지시 "agent_id·cluster_uid 일치 Agent만" |
| N3e Agent 실행 | `cmp_providers.agent_preflight.PreflightJobRunner`가 `ClusterTarget.in_cluster`의 kubectl로 기존 `KubernetesProbeExecutor`를 실행한다. 검사 Namespace에 `cmp.dev/environment-id` 주석이 없으면 Pod를 만들지 않는다(`NAMESPACE_NOT_OWNED`, 롤아웃·터널과 같은 소유 기준). 이미지는 Agent 설정(`--preflight-image`·`--preflight-allowed-images`, Chart `networkPreflight`)에서만 오며 Digest 고정·허용 목록이 아니면 `PREFLIGHT_NOT_CONFIGURED`. Kubernetes 접근 불가는 `UNKNOWN PROBE_LOCATION_UNAVAILABLE`로 바로 보고한다. Agent 로그에는 코드만 남는다. | N2 그대로 |
| N3f RBAC | `networkPreflight.image`가 있을 때만 ClusterRole에 `pods` create/delete, `pods/log` get. 대상 Namespace가 실행 시점에 생기므로 클러스터 범위다. `hostNetwork` Pod 생성도 가능해지는 점은 설치 문서 §4에 적었다. | 지시 "최소 RBAC" |
| N3g Worker 경로 선택 | `probe_target.agent_id`가 업무 DB에 등록된 온프레미스 Agent면 Agent 작업, 아니면 기존 명시적 kubeconfig 경로. 등록 조회 실패는 `UNKNOWN AGENT_REGISTRY_UNAVAILABLE`로 끝나며 다른 위치로 조용히 바꾸지 않는다. 대기 한도 = 요청 `timeout_seconds` + 240초(검사 Pod·라우트 Pod·폴링 여유). 한도 초과는 QUEUED면 `AGENT_OFFLINE`, RUNNING이면 `AGENT_RESULT_PENDING`이고 작업은 남겨 다음 호출이 재사용한다. Heartbeat는 기존처럼 20초. `may_start_data_migration`은 바꾸지 않았다. | 등록 Agent는 정의상 ON_PREM이라 계약에 provider 필드를 더하지 않았다(Fingerprint·공유 계약 불변) |
| N3h 이전 Agent·이벤트 | 이전 Agent(`v0.2.0`까지)는 모든 작업을 `PRODUCTION_ROLLOUT`으로 검증해 `FAILED UNSUPPORTED_JOB`을 보고한다 → `UNKNOWN AGENT_JOB_UNSUPPORTED`. 다른 Agent 거절 코드는 그대로 UNKNOWN 코드가 된다. Activity는 결과가 나오면 `data.preflight`(`phase: data_copy`, PASS면 `done`, 아니면 `failed`, `may_start` 포함)를 `emit_event`로 낸다(두 경로 모두). | 지시. 이벤트 실패는 Activity를 실패시키지 않는다(24 E9) |

새 UNKNOWN 코드(N5 추가): `AGENT_OFFLINE`, `AGENT_RESULT_PENDING`, `AGENT_JOB_UNSUPPORTED`, `AGENT_NOT_REGISTERED`, `AGENT_REVOKED`, `AGENT_TARGET_MISMATCH`, `AGENT_REGISTRY_UNAVAILABLE`, `AGENT_RESULT_INVALID`·`AGENT_RESULT_MISSING`, `NAMESPACE_NOT_OWNED`, `CLUSTER_MISMATCH`, `PREFLIGHT_NOT_CONFIGURED`. 모두 시작을 막는다.

### 변경 위치

- 계약: `cmp_contracts.network_preflight` (`NetworkPreflightJob`, `preflight_job_id`). `ProbeLocationUnavailable(code)`로 실행 위치 불가의 원인 코드를 전달.
- 플랫폼: `cmp_api.agent_preflight`(큐·재사용·수신 검사·보고 검증), `agent_registry` 수신 gate·보고 분기, `routes/agents.py` `JobReport.observation`·`ClaimedJob.payload`에 `NetworkPreflightJob` 추가(OpenAPI 갱신, 추가만).
- Agent: `cmp_providers.agent_preflight`, `onprem_agent.RolloutAgent` 작업 유형 분기, `agent_runtime` 인자, `remote_agent.report(observation=)`, Chart `networkPreflight`·RBAC.
- Worker: `activities/network_preflight.py`(`AgentJobExecutor`, `DbAgentPreflightTransport`, `choose_executor`), `cmp_worker.preflight_events`.

### 검증 기록 (N3)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_network_preflight_agent.py` | 30개 통과. 계약 바인딩, 참조만 담은 작업·중복 재사용, 미등록·폐기·클러스터 불일치 거절, 다른 Agent 수신·보고 거절, 수신 시 클러스터 재확인, 보고→재조회(창 안)·재큐(창 밖)·재보고 수락, 보고 형태 검사(관측값 없음/잘못된 코드/추가 필드/크기), 이전 Agent 거절 수락, HTTP 수신·보고, Agent 실행(관측값만 보고·로그 코드만)·실행 전 거절 6종, Activity: Agent 작업 PASS와 `data.preflight` 이벤트(웹 계약 검증), 응답 유실 뒤 재사용, 오프라인 → `AGENT_OFFLINE`(작업 유지), 수신 후 무응답 → `AGENT_RESULT_PENDING`, `UNSUPPORTED_JOB` → `AGENT_JOB_UNSUPPORTED`, 미등록 Agent는 직접 경로, CIDR 실패는 작업 없음, 조회 실패 → `AGENT_REGISTRY_UNAVAILABLE`. 결과·이벤트·로그·작업 행에 비밀번호 없음 |
| `uv run pytest tests/test_onprem_agent_chart.py` | 24개 통과(추가 3: 기본은 Pod 생성 권한 없음, 이미지 지정 시 `pods` create/delete·`pods/log` get과 Agent 인자, Digest 아닌 이미지 거절) |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff·전체 1573개 통과, 8개 건너뜀 |
| `make openapi` | `ClaimedJob.payload`에 `NetworkPreflightJob`, `JobReport.observation`(`ProbeObservation`)과 관련 스키마 추가만. 기존 경로·필드 변경 없음 |
| 격리 k3d `cmp-mnp-agent`(K3s v1.36.4, ARM64) + 이 브랜치로 빌드한 Agent 이미지(Chart 설치, 터널 끔) + 원본 역할 PostgreSQL 16 + 프로세스 내 HTTPS 플랫폼 API + 실제 `run_network_preflight` Activity (`scripts/smoke_onprem_preflight.py`) | Agent가 HTTPS로 등록·수신하고 ServiceAccount로 검사 Pod를 만들었다. 정상 → PASS(4 MiB, 0.04초, 시작 허용, 전체 6.1초). 같은 요청 재호출 → 저장된 관측값 재사용(작업 1개 유지, 0.0초). 잘못된 비밀번호 Secret → `AUTH_FAILED`(FAIL, 시작 차단). 소유하지 않은 Namespace → `UNKNOWN NAMESPACE_NOT_OWNED`, 그 Namespace에 Pod 0개. 검사 Pod 잔존 0, `data.preflight` 4건(done·done·failed·failed), Agent 로그·이벤트·증거·작업 행에 비밀번호 없음. 증거: [mnp-01-agent-k3d.json](../../docs/verification/mnp-01-agent-k3d.json). 정리: Release·Namespace 삭제, k3d 클러스터 삭제, 기본 kubeconfig 해시 동일 |

### N3 미검증

- 이전 Agent(`v0.2.0` 이미지)가 실제로 `UNSUPPORTED_JOB`을 보고하는 것은 같은 코드 경로를 가짜 Agent로만 확인했다(실제 이미지로 돌리지 않음).
- 실제 노트북 K3s + WireGuard 터널 위 원본 AWS DB 경로(2026-11-06 Traffic 담당 합동 시험 예정)에서는 실행하지 않았다. k3d에서는 터널 인터페이스 기대값 없이 Service CIDR을 원본 쪽으로 대신 썼다.
- 운영 PostgreSQL 업무 DB(SQLite 아님)에서 동시 제출 경합은 기존 롤아웃과 같은 `IntegrityError` 재조회 방식에 의존하며 따로 시험하지 않았다.
- 새 Agent 이미지 릴리스는 승인 대기(태그 안 함). 데이터 담당이 Workflow에서 Activity를 호출하는 연결은 담당 범위다.
