# Ticket 75 — 온프레미스 데이터 이전용 kubeconfig·ServiceAccount (ADR-007 2안 5단계)

## 상태
IN_PROGRESS (로컬 구현·시험·k3d 실측 완료, PR 리뷰 대기. 중계 서버 경유 실측은 4단계(74) 뒤) — [Issue #304](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/304). 2026-11-07 조율 세션 지시로 착수.

## 배경
[ADR-007](../../docs/decisions/ADR-007.md) 2안에서 데이터 이전 worker는 ON_PREM 대상에 `target_kubeconfig_ref`(access=KUBECONFIG)를 쓰고 `DATA_MIGRATION_KUBECONFIG_DIR/<ref>.yaml`을 읽는다(#300). 경로는 worker → 중계 서버(4단계, 74) → Agent 터널 IP:6443 → `kubernetes.default.svc:443`([73](73-agent-kube-api-port.md)). 이 티켓은 그 kubeconfig에 넣을 **좁힌 ServiceAccount**와 발급 도구다. cluster-admin을 쓰지 않는다.

## 관련 요구사항
PROV-021, DM-001, DM-011, ADR-007, [72](72-data-source-fixes.md), [73](73-agent-kube-api-port.md).

## 작업 범위
- 포함: 권한 표 도출, RBAC, 사용자 맥에서 실행하는 발급·확인·삭제 도구 `infra/onprem-agent/make-data-kubeconfig.py`, 문서
- 제외: 중계 서버 설정(74), Agent 전달기, 데이터 Activity 동작 변경, 서버 파일 복사·배포(승인 필요), 웹 시작 입력 연결(7단계), Agent Chart 연동(후속)

## 권한 표 (대상 K3s에만, 데이터 Activity 코드 기준)

kubeconfig는 **대상** 접근에만 쓴다. DB 전환(`data_cutover` install·route_app·switch의 PgBouncer·실행기)은 항상 `OPEN_CLUSTER(req.source)`라 원본(EKS) 자격 증명을 쓴다. 대상에는 `OPEN_DB`(psql exec)와 데이터 게이트웨이만 닿는다.

| 범위 | 리소스 | 동사 | 코드 (호출) |
| --- | --- | --- | --- |
| 대상 앱(DB) Namespace | `pods` | get, list, watch, delete | `KubectlDbPod.restart`(`delete pod --wait`, `wait --for=condition=Ready`), exec 전 Pod 조회 |
| | `pods/exec` | create | `KubectlDbPod._exec`(psql·pg_dump·restore·`login`), `switch`의 대상 login·역할 SQL |
| | `secrets` | get, create | `read_secret`·`create_secret`(구독 `cmp-repl` 비밀번호, forward 구독자·reverse 발행자) |
| | `networkpolicies.networking.k8s.io` | get, create, patch, delete | `data_tunnel.establish`의 `db_ingress_policy` SSA, `cleanup`의 `delete --wait` |
| `cmp-data-tunnel` | `configmaps` | get, create, patch, delete | 게이트웨이 ConfigMap SSA·정리 |
| | `services` | get, create, patch, delete, list, watch | `{base}-peer-db`(·`ingress=target`이면 입구) SSA, `lb_address`, 정리 `delete --wait`(LB finalizer 대기) |
| | `deployments.apps` | get, create, patch, delete, list, watch | 게이트웨이 SSA, `rollout status`, 정리 |
| | `secrets` | get, create, update, delete | `_key`(`secret_data`, `put_secret` create/replace), 정리 |
| | `pods` | get, list | `pod_for`(`-l` 조회), exec 전 조회 |
| | `pods/exec` | create | `latest_handshake`(`wg show`) |
| 클러스터 | `namespaces` (`resourceNames: [cmp-data-tunnel]`) | get, patch | `establish`의 `namespace_manifest()` SSA. Namespace는 도구가 미리 만든다 |

표에 없는 것: Namespace 생성·삭제, 노드, 다른 Namespace, Secret list, RBAC 객체. `kubectl get --raw /readyz`와 discovery는 기본 `system:public-info-viewer`·`system:discovery`로 된다.

## 착수 결정 (2026-11-07, Issue #304)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| R1 | SA 위치 | 전용 Namespace `cmp-data-migration`의 `cmp-data-migration` (`automountServiceAccountToken: false`) | Agent(`cmp-system`)와 수명을 분리한다. `helm uninstall`로 데이터 권한이 같이 사라지거나, 반대로 Namespace 삭제로 Agent가 흔들리지 않는다. Namespace 삭제 한 번으로 SA와 발급 토큰이 무효가 된다 |
| R2 | `cmp-data-tunnel` 생성 | 도구가 데이터 코드와 같은 라벨로 미리 만들고, worker에는 그 이름의 get·patch만 준다 | worker의 `namespace_manifest()` SSA는 그대로 통과하고 클러스터 범위 Namespace create가 필요 없다. RoleBinding을 둘 곳도 필요하다 |
| R3 | 대상 앱 Namespace | 만들지 않는다. 있으면 Role을 두고, 없으면 나머지만 적용해 kubeconfig를 쓰고 `APP_NAMESPACE_PENDING`(종료 코드 3)을 낸다. 배포 뒤 `bind`(선택 `--wait-namespace`)로 붙인다 | Agent는 `cmp.dev/environment-id` 주석이 없는 기존 Namespace를 `NAMESPACE_NOT_OWNED`로 거절한다(15 §4). 데이터 Activity는 권한 거부(`DB_COMMAND_FAILED`)를 재시도한다. Namespace를 지우면(52 정리) Role도 같이 사라진다 |
| R4 | 최소 동사 | `pods/exec`은 create만, 즉시 사라지는 ConfigMap·Secret·NetworkPolicy는 list·watch 없음. 지켜보는 Pod·Service·Deployment에만 list·watch | k3d(k3s 1.36.4, kubectl 1.37.1) 실측: exec는 create만으로 되고 get만으로는 거부, `delete --wait`는 바로 사라지는 객체에 list·watch 없이 끝난다. `wait`·`rollout status`는 watch가 필요하다 |
| R5 | 토큰 | `kubectl create token --duration`(기본 12h) bound 토큰. 장기 Secret 토큰은 만들지 않는다. 만료 시각만 출력한다 | 데모 리허설·본 데모를 덮고 끝나면 스스로 만료된다. 끝나면 `delete`로 SA를 지워 즉시 무효 |
| R6 | kubeconfig | `server: https://<중계 서버 주소>:<포트>`(인자), `certificate-authority-data`는 `kube-root-ca.crt` ConfigMap, `tls-server-name: kubernetes.default.svc`, `insecure-skip-tls-verify` 없음. JSON으로 쓴다(YAML의 부분집합) | 73 K8. 로컬 kubeconfig CA가 아니라 클러스터가 Pod에 주는 CA를 쓴다 |
| R7 | 파일·출력 | `<out-dir>/<ref>.yaml`(기본 `~/.config/cmp/data-migration`, 디렉터리 0700), 파일 0600(이미 있으면 좁힘). 토큰은 화면·오류에 내지 않는다. kubectl 실패는 동사만 알린다 | 저장소 밖 기본 경로라 실수 커밋을 막는다 |
| R8 | 적용 방식 | `kubectl apply --server-side --force-conflicts --field-manager cmp-data-kubeconfig` | 멱등. 손으로 고친 Role도 표대로 되돌린다(실측에서 수동 patch 뒤 충돌 확인) |
| R9 | 확인 | `verify`: 관리자 context의 impersonation `auth can-i`로 표 전체 허용 + 대표 거부 11개. 서버에서는 `kubectl --kubeconfig … get --raw /readyz`·`auth can-i --list` | 맥에서는 중계 서버 주소에 닿지 않으므로 RBAC는 impersonation으로, 경로는 서버에서 확인한다 |
| R10 | Chart 연동 | 이번에는 별도 스크립트. Agent Chart `dataMigration.serviceAccount.enabled`는 후속 | 데모 일정. Chart로 넣으면 앱 Namespace RoleBinding(R3)을 Agent가 배포 때 만들어야 하고, Agent에 RBAC 생성 권한(`escalate`/`bind`)이 필요해져 15 H2 권한이 커진다 |
| R11 | 위치·언어 | `infra/onprem-agent/make-data-kubeconfig.py`, 표준 라이브러리만 | 사용자 맥에서 `python3`로 바로 돈다. 시험은 `importlib`로 불러온다(`infra/demo/approver-token.py`와 같다) |

## 공개 인터페이스

```text
make-data-kubeconfig.py apply  --context C --namespace NS --server https://H:P --ref REF [--duration 12h] [--out-dir D]
  → KUBECONFIG_WRITTEN <path> (0600) expires <time>   종료 0 | 3(APP_NAMESPACE_PENDING) | 2(오류)
make-data-kubeconfig.py bind   --context C --namespace NS [--wait-namespace SEC]   → RBAC_APPLIED | APP_NAMESPACE_PENDING
make-data-kubeconfig.py verify --context C --namespace NS   → VERIFY OK n/n | MISSING… / UNEXPECTED_ALLOW…(종료 1)
make-data-kubeconfig.py delete --context C --namespace NS   → Role·ClusterRole·SA Namespace 삭제(cmp-data-tunnel은 남김)
```

## 수용 기준
- [x] RBAC가 권한 표와 빠짐없이·초과 없이 같다. `*` 없음, 바인딩은 SA 하나
- [x] 앱 Namespace를 만들지 않고, 없으면 kubeconfig는 쓰고 대기를 알린다
- [x] kubeconfig: `tls-server-name`, CA, 토큰. skip-verify 없음. 파일 0600, 토큰 미출력
- [x] k3d 실측: 이 kubeconfig로 worker 실제 코드(`KubectlDbPod` exec·Secret·restart, `TunnelCluster` SSA·rollout·exec·delete --wait) 22단계 통과, `kube-system` Secret 거부, `verify` 49/49, `/readyz` ok
- [ ] 중계 서버(74) 경유 `kubectl --kubeconfig … get --raw /readyz` — 4단계 뒤
- [ ] 실제 데모 이전에서 데이터 Activity 전체 — 7단계 뒤

## 시험
`tests/test_data_migration_kubeconfig.py`(권한 표 일치, 와일드카드·바인딩, Namespace 소유, 잘못된 Namespace·서버·ref, kubeconfig 형식, 0600·토큰 미출력, 앱 Namespace 대기, bind·delete·verify). `make check`. HTTP·Temporal 변경 없음(openapi·smoke-temporal 해당 없음).

### k3d 실측 (2026-11-07, `k3d-cmp-local`)
시험용 Namespace `cmp-dm75-check`에 postgres:16 StatefulSet을 두고 `apply --server https://127.0.0.1:<k3d 포트>`(k3d 인증서 SAN에도 `kubernetes.default.svc`가 있다)로 발급했다. 결과는 위 수용 기준. 확인 뒤 `delete`, `cmp-dm75-check`·`cmp-data-tunnel` Namespace를 지웠고 남긴 것은 없다. 기존 `cmp-app`·`cmp-system`은 건드리지 않았다.

## 남은 일
- `--server https://<중계 서버 사설 IP>:<16400+터널 IP 마지막 옥텟>`(74 R4)로 발급, 서버 `/opt/cmp/secrets/data-migration/<ref>.yaml`로 복사(승인 필요), 서버에서 `/readyz` 확인
- Agent Chart 연동(R10), 7단계 웹 시작 입력의 `target_kubeconfig_ref`
