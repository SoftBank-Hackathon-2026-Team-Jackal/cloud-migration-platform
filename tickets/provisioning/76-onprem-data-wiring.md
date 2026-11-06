# Ticket 76 — 온프레미스 데이터 이전 Workflow 연결(터널·사전 점검·정리) (ADR-007 2안 7단계)

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰 대기. 실제 EKS NLB ↔ 맥 k3d 실측 남음) — [Issue #309](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/309). 2026-11-07 조율 세션 지시로 착수.

> **데이터 영역 대행.** 데이터 Workflow 연결은 데이터 담당 영역이다. Provisioning(김민서)이 대행하며 복제 단계(precheck·prepare·schema·subscribe·sample·전환) 내부는 바꾸지 않았다.

## 배경
[ADR-007](../../docs/decisions/ADR-007.md) 2안 조각(입구 방향 [67](67-data-tunnel-reverse-ingress.md), 원본 쪽 수정 [72](72-data-source-fixes.md), worker → k3d 길 [73](73-agent-kube-api-port.md)·[74](74-relay-kube-api-forward.md)·[75](75-data-migration-kubeconfig.md))은 main에 있다. 남은 고리는 Workflow였다: 터널을 연 뒤 [MNP](../../specs/migration-network-preflight/spec.md) 사전 점검으로 막고, 끝나면 원본 EKS의 공개 NLB를 지운다.

## 관련 요구사항
DM-011, MNP-001~006(N1·N4), ADR-007, data-migration 티켓 08·10.

## 작업 범위
- 포함: `DataMigrationWorkflow` 순서(터널 → `run_network_preflight` → `may_start_data_migration` → 기존 복제)와 끝 정리(`dm_tunnel_cleanup`), 두 `workflow.patched`, `cmp_data.handoff.preflight_request`, 부모 `MigrationWorkflow`의 요청 생성·전달, 웹 시작 ON_PREM 검증, 문서
- 제외: 복제 단계 내부, 게이트웨이 매니페스트, Agent·중계 서버, 웹 화면·report(8단계), 서버 반영·AWS 변경, 검사 실행기 선택 변경

## 착수 결정 (2026-11-07, Issue #309)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| W1 | 사전 점검 실행 조건 | `data_migration.network_preflight`가 있으면 실행한다. 웹 시작에서 ON_PREM + `tunnel.ingress="source"`인데 없으면 503 `DATA_MIGRATION_PREFLIGHT_REQUIRED`(= ON_PREM 기본 켬) | 입력이 있을 때만 하면 EKS↔GKE 기존 실행이 그대로다. ON_PREM은 확인 안 된 경로로 복제하지 않는다. 원본 VPC CIDR은 기본값을 둘 수 없어 "설정 없으면 기본값으로 켬"은 불가 |
| W2 | 순서 | 터널 → 사전 점검 → precheck… | 검사 주소가 게이트웨이 Service라 터널이 먼저 있어야 한다 |
| W3 | 게이트 | `may_start_data_migration`(같은 입력 Fingerprint의 PASS만). 막히면 `blocked` + `NETWORK_PREFLIGHT_FAIL`·`NETWORK_PREFLIGHT_UNKNOWN`·`PREFLIGHT_INPUT_CHANGED`. Activity 오류·결과 해석 실패도 `NETWORK_PREFLIGHT_UNKNOWN` | 판정은 MNP 공통 함수 하나(N4). 웹 원인은 Activity가 내는 `data.preflight` 이벤트(`preflight_events.py`)와 부모의 `failure_code`로 본다 |
| W4 | 터널 정리 | 터널 단계에 들어간 실행은 `_finish`(done·failed·cancelled·blocked, 롤백 뒤 finish 포함)에서 슬롯 정리 **뒤** `dm_tunnel_cleanup`을 한 번 부른다. 터널 만들기가 실패해도 부른다(NLB가 이미 생겼을 수 있다). 실패는 `tunnel_cleanup_code`에만 남긴다 | 슬롯 정리(`dm_cleanup`)는 터널로 원본에 닿는다. 롤백이 터널을 쓰므로 스트리밍 중에는 지우지 않는다. continue-as-new 상태에 `tunnel_made`를 넘긴다 |
| W5 | 이력 호환 | `data-network-preflight-v1`, `data-tunnel-cleanup-v1` | 패치 전 이력은 사전 점검·정리 없이 재생된다(시험) |
| W6 | `source_db_host` | `<cmp-wg-…>-peer-db.cmp-data-tunnel.svc.cluster.local:5432`, `database`=원본 `dbname` | 대상 DB가 원본에 닿는 실제 주소(N1) |
| W7 | `tls_mode` | 기본 `disable`(설정으로 `require`·`verify-full`) | WireGuard가 암호화한다. 복제 구독 conninfo도 sslmode를 정하지 않고 데모 원본 PostgreSQL은 TLS가 없다 |
| W8 | `db_auth_ref` | 설정에서 받는다(검사 Namespace의 Secret 이름·키). 운영자가 미리 둔다 | 복제 역할 Secret(`cmp-repl-…`)은 `dm_subscribe`에서야 생기고 키도 `password` 하나뿐이며 원본 `cmp_repl` 역할도 그때 생긴다. 자동 생성은 복제 단계 변경이라 후속 |
| W9 | 경로 범위 | `allowed_private_destination_cidrs` 기본 `10.43.0.0/16`(k3s Service), `onprem_routed_cidrs` 기본 `10.42.0.0/16`,`10.43.0.0/16`, `tunnel_cidrs` 기본 `10.210.0.0/24`, `aws_vpc_cidrs` 필수, `tunnel_interface` 비움 | 대상 쪽에서 원본 DB는 게이트웨이 ClusterIP로 보인다. 노드에는 wg 장치가 없다(게이트웨이 Pod 안) |
| W10 | `probe_target` | 인계값 `common.cluster.agent_id`·`cluster_uid`, Namespace 기본 대상 DB Namespace(`probe_namespace`로 바꿈), `node_name` 기본 없음(설정 가능). 없으면 부모가 자식을 시작하지 않고 `NETWORK_PREFLIGHT_TARGET_MISSING` | 단일 노드 k3d. DB Pod 노드 조회는 I/O라 후속 |
| W11 | 실행기 | 바꾸지 않는다(`choose_executor`: 등록 Agent면 Agent 작업, 아니면 `PREFLIGHT_KUBECONFIG`) | 범위 밖 |
| W12 | `target_egress_cidrs` | 설정값(맥 공인 IP/32) | 후속: 중계 서버가 본 Agent handshake 주소로 자동화 |
| W13 | NLB 준비 | 시작 때 `dm_tunnel`이 만들고 handshake까지 기다린다(약 2분 30초, 시간 제한 3600초) | PoC 69 실측 |

## 수용 기준
- [x] PASS → 복제 진행, FAIL/UNKNOWN → `blocked` + 원인 코드, 복제 Activity 없음, 터널 정리
- [x] 다른 입력의 PASS 재사용 거부(`PREFLIGHT_INPUT_CHANGED`)
- [x] 완료·실패·취소·차단·롤백 뒤 완료에서 터널 정리 한 번, 슬롯 정리 뒤, 정리 실패는 결과를 덮지 않음
- [x] 패치 전 이력 재생 호환, 새 이력 재생
- [x] 웹 ON_PREM 시작 입력에 `tunnel`·`target_kubeconfig_ref`·`network_preflight`가 실림, 없으면 거부
- [ ] 실측: 원본 EKS NLB ↔ 맥 k3d에서 PASS → 복제 → finish → NLB 삭제 확인

## 시험
`tests/data/test_data_workflow_onprem.py`, `tests/data/test_data_preflight_request.py`, `tests/data/test_data_parent.py`, `tests/test_web_start_request.py`. `make check`.

## 후속
- `db_auth_ref` Secret 자동 준비(데이터 담당과 협의), DB Pod 노드 자동 조회
- `target_egress_cidrs` 자동화(중계 서버가 본 Agent handshake 주소)
- Temporal 실행 취소·종료(terminate) 때는 정리하지 않는다 — 운영자가 `dm_tunnel_cleanup`과 같은 입력으로 정리
