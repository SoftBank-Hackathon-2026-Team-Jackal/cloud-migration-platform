# Ticket 12 — 원본 EKS 데이터 복제 전용 쓰기 역할

## 상태

코드·문서 완료. AWS 적용과 `kubectl auth can-i` 확인은 아래 「적용과 확인」 결과를 채운 뒤 DONE.

## 결론

- 데이터 이전은 원본 EKS(`cmp-demo-source`)에 **쓰기**가 필요하다(DB Pod exec, Secret 생성·교체, 게이트웨이·cmp-cutover 배포, 앱 재시작).
- 원본 **연결 역할**(`cmp-source-915ac0a11f34-ConnectionRole`, `AmazonEKSViewPolicy`)은 "연결은 읽기 전용" 원칙 때문에 **바꾸지 않는다**.
- 대신 데이터 전용 IAM 역할 **`cmp-demo-data-migration`**을 따로 둔다. 쓰기는 Namespace 두 개(`chat-source`, `cmp-data-tunnel`)에서만 된다.
- 데모 worker는 AWS 프로필 **`cmp-demo-data-migration`**(이름 고정)으로 이 역할을 쓴다. compose의 `DATA_MIGRATION_AWS_PROFILE=cmp-demo-data-migration`은 다른 작업(작업 6) 몫이다.

## 코드에서 확정한 필요 권한

원본 쪽에서 실제로 부르는 것만 적었다(`apps/worker/src/cmp_worker/activities/data_*.py`, `packages/data/src/cmp_data/`).

| 동작 | 어디서 | Namespace |
| --- | --- | --- |
| `aws eks update-kubeconfig` → `eks:DescribeCluster` | `CliRuntime.connect`(`DATA_MIGRATION_AWS_PROFILE`) | — |
| `aws eks get-token` | kubeconfig exec | IAM 동작 없음(STS 서명) |
| `pods/exec`(psql), `pods` delete·get·list, `wait` | `KubectlDbPod` | chat-source |
| `secrets` get·create | DB 비밀번호 Secret | chat-source |
| `secrets` get·create·replace, `deployments` get·rollout restart·status, `pods` list, `replicasets`(rollout status), Deployment·Service server-side apply, `pods/exec`(runner) | cutover(`data_cutover.py`, `cmp_data.cutover`) | chat-source |
| NetworkPolicy apply(DB 5432 허용) | `wg.db_ingress_policy` | DB가 있는 Namespace |
| ConfigMap·Deployment·Service(2안의 UDP LoadBalancer 포함)·Secret apply, `pods/exec`(`wg show`) | 터널(`data_tunnel.py`, `cmp_data.tunnel`) | cmp-data-tunnel |
| **Namespace `cmp-data-tunnel` server-side apply**(PATCH, 클러스터 범위) | `cluster.apply([wg.namespace_manifest()])` | — |

`AmazonEKSEditPolicy`(Namespace 범위)가 마지막 줄을 뺀 전부를 덮는다. Namespace 객체는 클러스터 범위라 편집 정책으로는 읽기만 된다. 그래서:

- Namespace `cmp-data-tunnel`은 운영자가 먼저 만든다(라벨은 `namespace_manifest()`와 같음).
- 접근 항목에 Kubernetes 그룹 `cmp-data-migration`을 붙이고, 그 그룹에 **이 Namespace 하나**의 get·patch만 준다(`infra/demo/data-migration-rbac.yaml`).

## 만든 것

| 파일 | 내용 |
| --- | --- |
| `infra/demo/data-migration-stack.yaml` | 별도 스택 `cmp-demo-data-migration`. IAM 역할 `cmp-demo-data-migration`(신뢰: 데모 인스턴스 역할만, ExternalId 없음, 인라인 `eks:DescribeCluster` 이 클러스터만), EKS 접근 항목(`AmazonEKSEditPolicy`, namespace 범위 `chat-source`·`cmp-data-tunnel`, 그룹 `cmp-data-migration`), 인스턴스 역할 인라인 정책 `cmp-demo-data-migration`(이 역할 AssumeRole만) |
| `infra/demo/data-migration-rbac.yaml` | Namespace `cmp-data-tunnel`, 위 get·patch ClusterRole·Binding |
| `infra/demo/deploy-data-migration-stack.sh` | `--plan`(변경 세트만) / 적용. 적용은 스택 배포 → 임시 kubeconfig(기본 `~/.kube/config` 안 씀)로 RBAC 적용 → `.env` 줄 출력 |
| `infra/demo/ensure-secrets.sh` | `.env`에 `DATA_MIGRATION_ROLE_ARN`이 있으면 `[profile cmp-demo-data-migration]`(`role_arn` + `credential_source = Ec2InstanceMetadata`, `source_profile` 없음 — #265) |
| `tests/test_data_migration_source_access.py` | 템플릿 범위, RBAC, 프로필 생성 |

인스턴스 역할은 `cmp-demo-control-plane` 스택 소유지만 그 스택은 갱신하지 않는다(미반영 EIP 때문에 공인 IP가 바뀐다, 빌드 스택과 같은 이유). 그래서 AssumeRole 허용은 이 별도 스택의 `AWS::IAM::RolePolicy`로 붙인다.

## 적용과 확인

```sh
# 1) 확인용으로 운영자 역할도 잠시 신뢰하게 적용
CMP_DATA_VERIFIER_ARN=arn:aws:iam::714066696240:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/<운영자 SSO 역할> \
  bash infra/demo/deploy-data-migration-stack.sh
# 2) 실제 역할로 assume → 임시 kubeconfig → kubectl auth can-i (chat-source·cmp-data-tunnel yes, default·kube-system no)
# 3) 확인용 신뢰를 지우고 다시 적용
bash infra/demo/deploy-data-migration-stack.sh
```

결과: (적용 뒤 기록)

## 서버 반영(운영, 이 티켓에서 안 함)

- `/opt/cmp/.env`에 `DATA_MIGRATION_ROLE_ARN=arn:aws:iam::714066696240:role/cmp-demo-data-migration`
- compose worker에 `DATA_MIGRATION_AWS_PROFILE=cmp-demo-data-migration`(작업 6). worker는 api가 쓰는 공유 연결 설정 파일을 읽으므로 api가 다시 시작된 뒤 프로필이 보인다(B-23, ECR pull 프로필과 같음)
- 확인: `docker exec cmp-demo-worker-1 aws sts get-caller-identity --profile cmp-demo-data-migration --query Arn --output text` → `.../assumed-role/cmp-demo-data-migration/...`
