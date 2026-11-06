# Ticket 51 — ON_PREM 배포 실패 시 기존 앱·DB 보존과 복구

## 상태
IN_PROGRESS (로컬 구현·테스트·k3d 실측 완료, 리뷰 대기) — [Issue #186](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/186). 우선순위 높음(P1), 담당 민서. 15·27 후속. **새 Agent 릴리스가 필요하다(코드·RBAC 변경, 태그는 사용자 승인 뒤).**

## 배경 (점검 C-02, 2026-11-06 로컬 k3d 실재현)
- rev1이 READY인 상태에서 노드 아키텍처와 다른 rev2를 배포하면, Agent가 DB Release를 먼저 `helm upgrade --wait`한다. StatefulSet 갱신으로 기존 `postgres` Pod가 지워지고 새 Pod는 `FailedScheduling`이라 DB가 내려간다. 앱도 0/1이 된다. Helm 이력은 rev1 `deployed`, rev2 `failed`다.
- Agent는 되돌리지 않는다. 이후 배포는 Release 상태가 `failed`라 모두 `UNKNOWN INSPECTION_REQUIRED`로 멈추고, 운영자 복구 수단도 없다.
- 이번 착수 전 k3d(K3s v1.36.4)에서 추가로 확인했다: replica 1 StatefulSet은 스펙을 이전 Revision으로 되돌려도 **실패 Revision의 Pending Pod를 기다리며 멈춘다.** 그 Pod를 지워야 이전 Revision Pod가 다시 뜬다(쿠버네티스 StatefulSet의 알려진 동작). 따라서 `helm rollback`만으로는 DB가 돌아오지 않는다.

## 관련 요구사항
PROV-009, PROV-011, PROV-016, PROV-017, PROV-018

## 선행 작업
12, 15, 27(DONE). 52(시한·취소·정리)가 같은 파일을 크게 바꿀 예정이다(아래 "함수 경계").

## 범위
- 포함: Agent rollout의 Helm 업그레이드 실패 시 직전 `deployed` Revision 복원, 고착 StatefulSet Pod 정리, `failed`로 남은 Release의 검사 후 재적용, 결과 코드 `ROLLBACK_FAILED`, Agent ClusterRole `pods` delete, 문서
- 제외: 노드 아키텍처 사전 대조(#155, C-03), 시한 초과·취소·`pending-*` 자동 정리(52), 실패 Revision 동안의 DB 중단 자체를 막는 것(사전 대조가 필요, 아래 R7), AWS/GCP 배포 경로(09/11)

## 수용 기준 (Issue #186)
- [x] 두 번째 배포 실패 뒤 기존 앱·DB가 계속 동작한다(k3d 재현 스크립트로 확인). — 실패 Revision 동안 DB Pod는 잠시 내려갔다가 rev1로 복원되며 데이터가 남는다(R7).
- [x] 실패 뒤 다음 배포를 진행할 수 있는 경로가 있다. — 복원 뒤 Release가 `deployed`라 다음 Revision이 그대로 적용된다. 이전 Agent가 `failed`로 남긴 Release도 다음 배포가 검사 후 복원·재적용한다.

## Test Seam
- `RolloutRunner.rollout(job)` + 12의 `FakeCluster`를 확장한 `HistoryCluster`(Helm 이력·rollback·고착 Pod 모델, 가짜 시계): `tests/test_onprem_rollout_recovery.py`
- `ClusterTarget.history` 출력 축약: `_exec` 대체
- 결과 분류: `cmp_providers.provisioning_result._checks`(ON_PREM)
- Agent Chart RBAC: `tests/test_onprem_agent_chart.py`(실제 `helm template`)
- 실제 Helm 4·K3s 동작: `scripts/smoke_onprem_rollout.py` 5·6단계(격리 k3d)
- 놓치는 것: 실제 Agent 이미지·in-cluster ServiceAccount 권한으로 실행(로컬 kubeconfig는 관리자 권한), Helm 3

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| R1 기다리는 주체 | `helm upgrade --install`에서 `--wait`를 뺀다. Agent가 `agent_rollout_recovery.settle`로 Workload가 정착(observedGeneration·ready·updated·replicas·unavailable)할 때까지 `--helm-timeout`(기본 5분) 동안 2초 간격으로 확인한다 | `--wait` 중에는 고착 Pod를 정리할 수 없다. `--atomic`/Helm 4 `--rollback-on-failure`는 되돌린 뒤에도 같은 StatefulSet을 기다리다 시한을 다 쓰고, 실패 원인 검사(verify)도 되돌린 뒤라 의미가 없다 |
| R2 고착 Pod 정리 | 정착을 기다리는 동안 StatefulSet이 관리하는 Pod 중 **Ready가 아니고** `controller-revision-hash`가 `status.updateRevision`과 다른 Pod만 `--wait=false`로 지운다. 컨트롤러가 새 generation을 관측하기 전에는 지우지 않는다. Ready Pod·PVC는 건드리지 않는다 | k3d 실측(배경). Ready가 아닌 Pod는 이미 서비스하지 않으므로 지워도 잃는 것이 없다. 데이터는 PVC(`resource-policy: keep`)에 있다 |
| R3 실패 시 복원 | 업그레이드가 정착하지 않으면 먼저 기존처럼 `verify`로 첫 실패 검사 코드를 얻고, 그다음 **정착하지 않은 그 Release만** 이 작업 전 `deployed` Revision으로 `helm rollback <release> <revision>`(wait 없음) → R2 정착 대기. 결과는 `FAILED <원인 코드>`(예: `WORKLOAD_NOT_READY`, `IMAGE_PULL_FAILED`). DB가 실패하면 앱은 업그레이드하지 않는다(기존 순서) | 원인 코드는 `cmp_contracts` 실패 분류 그대로(`FAILED` → `RETRY_AFTER_FIX`): 기존 Revision이 살아 있으니 고친 Revision을 다시 보내면 된다. DB만 성공하고 앱이 실패하면 DB는 새 Revision으로 둔다(정상 동작 중인 DB를 다시 재시작하지 않는다) |
| R4 복원 실패 | rollback 명령 실패·복원 정착 실패·클러스터 접속 실패는 `UNKNOWN ROLLBACK_FAILED`. ON_PREM 검사 `helm_releases`에 매핑(`CHECK_STATE_FIRST`) | 이전 상태를 보장할 수 없으므로 사람이 먼저 확인해야 한다. 새 코드는 비밀을 담지 않는다 |
| R5 첫 설치 실패 | 이전 `deployed` Revision이 없으면 되돌리지 않는다(보존할 앱이 없다). Release는 Helm 4에서 `deployed`(표식=실패 작업)로 남고, 다음 Revision이 표식이 달라 다시 업그레이드하며 R2가 고착 Pod를 정리한다 | 같은 작업이 다시 수신되면(보고 유실) 같은 표식이라 검증만 하고 같은 실패 코드를 보고한다(같은 바이트 재시도 무의미) |
| R6 `failed`로 남은 Release | 다음 배포가 Release 상태 `failed`를 보면 `helm history`(번호·상태만 읽음)에서 마지막 `deployed` Revision을 찾아 R3과 같이 복원한 뒤 새 Revision을 적용한다(검사 후 재적용). 없으면 그대로 `upgrade --install`(Helm이 실패 이력 위 업그레이드를 허용). `pending-*`·`uninstalling`은 지금처럼 `UNKNOWN INSPECTION_REQUIRED`로 손대지 않는다 | `failed`는 Helm 단계가 끝난 확정 상태다. 이전 Agent(v0.3.0 이하)가 남긴 C-02 상태를 별도 API 없이 다음 승인 Revision으로 복구한다. 진행 중일 수 있는 `pending-*`의 판단(시한·잠금)은 52 범위다. 운영자 수동 절차는 설치 문서 §6에 적었다 |
| R7 DB 중단 | replica 1·RWO PVC StatefulSet은 스펙이 바뀌면 기존 Pod를 먼저 지운다. 실패 Revision이 정착을 기다리는 동안(최대 `--helm-timeout`)과 복원 동안 DB는 내려가 있다. Chart 업데이트 전략은 바꾸지 않는다 | `OnDelete`로 바꿔도 새 스펙을 적용하려면 결국 같은 Pod를 지워야 해 중단 시간이 같다. 중단 자체를 막으려면 적용 전 대조(노드 아키텍처 #155, 용량)가 필요하다. 앱 Deployment는 replica 1에서 기본 `maxUnavailable=0`이라 기존 Pod가 남는다 |
| R8 권한 | Agent ClusterRole에 `pods` **delete만** 항상 추가(Preflight의 create와 별개). 사용 코드는 R2뿐이며 Agent 소유 Namespace의 관리 StatefulSet Pod로 한정된다 | 고착 Pod 정리에 필요한 최소 동사. `pods/eviction`은 PDB가 없어 이점이 없다 |
| R9 함수 경계 | 새 모듈 `cmp_providers.agent_rollout_recovery`(`settle`·`restore`·`last_deployed_revision`·`workload_settled`·`duration_seconds`). `onprem_agent`에서 바뀐 곳: `ClusterTarget.release`(revision 추가)·`history`·`now`·`pause`, `ReleaseNotSettled`, `RolloutRunner._install(job, kind, …)`(검사→업그레이드→정착), `RolloutRunner.rollout`의 Helm 단계 이후, `RolloutRunner._restore`, `_workload_ready`(판정만 공용 함수로) | 52가 같은 파일의 시한·취소를 고칠 때 대기 루프는 `settle` 하나만 바꾸면 된다(시한=`target.now()` 기준). 시계·sleep은 target에 두어 테스트가 가짜 시계를 쓴다 |

## 구현 위치
- `packages/providers/src/cmp_providers/agent_rollout_recovery.py`(신규)
- `packages/providers/src/cmp_providers/onprem_agent.py`(R9의 함수들)
- `packages/providers/src/cmp_providers/provisioning_result.py`(`ROLLBACK_FAILED` → `helm_releases`)
- `infra/helm/onprem-agent/templates/rbac.yaml`(R8)
- `scripts/smoke_onprem_rollout.py`(5·6단계)
- Chart `infra/helm/postgres`·`infra/helm/application`: 변경 없음(R7)

## 검증
- 단위: `tests/test_onprem_rollout_recovery.py`(20), 기존 Agent·Chart 테스트(`test_onprem_agent`, `test_onprem_agent_chart`, `test_database_url_rollout`, `test_env_provisioning`, `test_image_pull`) 통과
- RED: 새 테스트 10개가 구현 전 실패(`--wait` 사용, rollback 없음, `failed` → `INSPECTION_REQUIRED`)
- `make check`: 1868 passed, 8 skipped (lint·format 포함)
- k3d 실측(아래)

### k3d 실측 (2026-11-06)
k3d v5.9.0 / K3s v1.36.4+k3s1 / arm64 노드, Helm v4.3.1, 전용 kubeconfig, context `k3d-cmp-p51`, 로컬 레지스트리. `scripts/smoke_onprem_rollout.py` 결과(값 없는 증거):

[증거](../../docs/verification/provisioning-51-k3s.json)

| 단계 | 결과 |
| --- | --- |
| ①~③ 12 기존 단계(오프라인·재접속·보고 유실) | 그대로 통과(`UNKNOWN AGENT_OFFLINE` → `SUCCEEDED READY` → 재설치 없이 READY) |
| ④ pull 불가 앱 이미지 rev2 | `FAILED IMAGE_PULL_FAILED`(29). 앱 Release가 rev1로 복원(`superseded, superseded, deployed`), 앱 1/1·DB 1/1 유지 |
| ⑤ **C-02**: DB·앱 모두 amd64로 표시한 rev3(arm64 노드) | 106초 뒤 `FAILED WORKLOAD_NOT_READY`. DB Release rev1 내용으로 복원, DB Pod `Running/Ready`, 앱 Release는 손대지 않음(DB 실패 후 중단). 복원 직후 앱 0/1 → DB 재연결 뒤 1/1. ⑤ 전에 넣은 행 `c02_probe` 1건 유지 |
| ⑥ 원래 이미지의 rev4 | `INSPECTION_REQUIRED` 없이 `SUCCEEDED READY`, 행 1건 유지 |
| 정리 | Namespace 삭제, 기본 kubeconfig 불변 |

주: ⑤에서 DB는 실패 Revision 정착 대기와 복원 동안 내려가 있었다(R7). 106초 = 정착 대기 90초(`--helm-timeout 90s`) + verify + rollback·고착 Pod 정리·정착.

정리: `k3d cluster delete cmp-p51`, `k3d registry delete k3d-cmp-p51-reg`. 기본 kubeconfig 불변.

## 미검증
- 실제 Agent 이미지·in-cluster ServiceAccount(새 ClusterRole)로 실행: 새 Agent 릴리스 뒤 확인 필요
- Helm 3 CLI(Agent 이미지는 Helm 4.3.1 고정)
- 실패 Revision 동안의 DB 중단 시간 단축(R7, #155)
- `pending-*` 자동 판단(52)

## 문서 변경
- `specs/provisioning/onprem-shared-deployment.md` Agent 실행 항목
- `specs/provisioning/plan.md` §9 재시도 문단·§12 표
- `specs/provisioning/onprem-agent-install.md` §3·§4·§6
- `infra/helm/README.md` rollback 문장
- `specs/provisioning/shared-contracts.md`·OpenAPI·DB: 변경 없음(작업·보고 계약 그대로, 새 코드 값 하나)

## 완료 체크리스트
- [x] 티켓·tasks.md 행·Issue 연결
- [x] RED → GREEN 단위 테스트
- [x] k3d C-02 재현으로 보존·다음 배포 확인
- [x] SDD 문서 갱신
- [ ] 리뷰·병합(오케스트레이터)
- [ ] Agent 릴리스(사용자 승인)

## 후속 (52)
- 52 후속(#187): `pending-*`·`uninstalling`은 계속 `INSPECTION_REQUIRED`(D20)이며 정리 작업이 상태와 무관하게 uninstall한다. `pods` delete는 클러스터 범위로 두고 `watch`를 뺐다(D16). C-13(실패 Revision 동안 DB 중단)은 Preview 표시·#155로 넘기고(D18), C-15(앱만 실패하면 DB는 새 Revision)는 R3를 정책으로 확정했다(D19). C-14(복원 뒤 pull Secret 갱신)는 52에서 고쳤다(D17). [52](52-agent-job-lifecycle.md).
