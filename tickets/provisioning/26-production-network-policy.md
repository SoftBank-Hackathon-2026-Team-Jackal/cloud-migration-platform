# Ticket 26 — 운영 구축 NetworkPolicy 생성과 대상 정책 강제

## 상태
DONE (로컬 범위) — [Issue #150](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/150). 2026-11-06 조율 세션 지시로 착수 결정을 기록하고 구현했다. K3s 실제 강제는 k3d로 확인했다. EKS·GKE 실제 클러스터 강제 확인과 Agent 새 릴리스는 사용자 승인 대기다. 조율 세션 리뷰·PR 대기.

## 목적
분석은 `RequirementsV2.network_intent`를 만들어 구축까지 넘기고, 샌드박스는 같은 의도로 기본 차단·허용 정책을 실제로 만든다. 운영 구축은 의도의 리소스 ID만 검증하고 정책을 만들지 않았다. 운영 Chart에 NetworkPolicy가 없고, 대상 Terraform도 정책 강제를 켜지 않았다(원본 EKS는 VPC CNI `enableNetworkPolicy: true`로 강제한다). 그래서 PROV-013(DB 불필요 공개 금지, Provider별 네트워크 정책 차이 반영)을 충족하지 못했다. 분석 계획의 "각 워커가 환경 정책으로 변환" 합의를 운영 구축에서 구현한다.

## 관련 요구사항
PROV-010, PROV-011, PROV-013, PROV-014, PROV-015, PROV-016, PROV-021

## 선행 작업
Blocked by: 08, 09, 10, 11, 12, 18, 22 (DONE)

## 현재 구현과 재사용 지점
- `cmp_worker.sandbox_network.build_manifests`: 샌드박스 기본 차단·DNS·의도 간선·앱 진입 + 출구 프록시
- `cmp_worker.plan_publish.PlanPublisher`(18·22): ON_PREM Plan과 AWS/GCP `WORKLOADS` Plan 산출물·Preview·입력 Digest
- `ApprovedWorkloads`(09·11): 승인 산출물 Digest 재검증 → Namespace → Secret → Helm → Ingress/LB
- `RolloutRunner`(12): 승인 job의 산출물만 받아 읽기 점검 후 변경. 플랫폼 관리 라벨이 없는 같은 이름 Secret은 덮어쓰지 않는다(19)
- 운영 Chart의 Pod 라벨 `app.kubernetes.io/instance: <release>`(샌드박스와 같은 선택자)

## 범위
- 포함
  - 샌드박스의 의도 변환을 공통 모듈 `cmp_providers.network_policy`로 옮기고 샌드박스는 프록시 규칙만 따로 둔다(출력 바이트 동일)
  - 운영 정책 렌더: 기본 차단, DNS, 의도 간선, 앱 → DB, 외부 진입(D-15), `external_host` 출구, 플랫폼 운영 경로
  - Plan 산출물 `network_policies`(`MANIFEST`)를 manifest에 고정하고 Preview `changes`·`applied_defaults`에 표시
  - AWS/GCP 배포 Activity와 ON_PREM Agent가 Helm 전에 적용, 관리 라벨 없는 같은 이름 정책은 충돌, 승인에서 빠진 관리 정책은 제거
  - 대상 강제: EKS VPC CNI 애드온 `enableNetworkPolicy`, GKE Dataplane V2, K3s 내장 확인
  - D-15 결정, SDD·분석 계획 교차 참조·OpenAPI 갱신
- 제외: 분석 의도 추출 변경(분석 담당), 샌드박스 출구 프록시, 클러스터에만 있는 정책 수집(분석 범위 협의), 분석 `external_entries` → 노출 반영([32 #156](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/156)), 실제 클라우드 Apply, Agent 릴리스 태그

## 수용 기준 (Issue #150)
- [x] 세 대상에서 `network_intent`대로 정책이 만들어지고, 앱 → DB와 외부 진입은 통하며 의도에 없는 통신은 막힌다. (가짜 클러스터 평가기 세 대상 + K3s k3d 실제 강제)
- [ ] EKS·GKE 대상에서 정책이 실제로 강제된다. Terraform 설정·validate와 배포 전 강제 확인(`NETWORK_POLICY_NOT_ENFORCED`)까지 완료. 실제 EKS·GKE 클러스터 확인은 클라우드 Apply 승인 필요(BLOCKED)
- [x] 샌드박스와 운영이 같은 변환 함수(`base_policies`·`entry`)를 쓰고, 환경별 차이만 따로 둔다.
- [x] Preview에 정책 변경이 보이고, 승인 후 의도가 바뀌면 실행하지 않는다(의도 변경 → 입력 Digest 변경 → 새 Revision → 이전 승인은 `STALE_REVISION`).
- [x] 기존 배포 경로에 회귀가 없다(티켓 26 이전 Plan은 산출물이 없어 정책 단계를 건너뛴다. 전체 테스트 통과).

## Test Seam
- 변환: `production_policies` 결과를 작은 NetworkPolicy 평가기(선택자·Namespace·ipBlock·포트)로 판정한다. 허용(앱 → DB, LB/인터넷 → 앱 포트, DNS, 외부 의도, DB 복제·자기 점검, ON_PREM Agent 터널·Preflight)과 차단(다른 Pod·다른 Namespace → DB, 앱 다른 포트, 사설 대역, DB → 인터넷, 앱 → 다른 Pod)을 세 Provider로 확인
- 게시: ON_PREM·AWS·GCP Plan의 `network_policies` 산출물·Preview 줄, 의도 변경 시 새 Revision, rollout job의 산출물 참조
- 실행: 실제 09·11 Activity(가짜 클러스터)에서 Helm 전 적용·결과 refs, 강제 없음 → `NETWORK_POLICY_NOT_ENFORCED`, 관리 라벨 없는 같은 이름 → `NETWORK_POLICY_CONFLICT`, 승인에서 빠진 관리 정책 제거. Agent `RolloutRunner`도 같은 판정
- 실제 강제: `scripts/smoke_network_policy_k3d.py`(격리 k3d, Agent `RolloutRunner` 코드 경로로 적용) → [증거](../../docs/verification/prov-26-k3d.json)
- 놓치는 것: 실제 EKS·GKE 강제(클라우드 승인 필요), 실제 Agent 이미지로 적용(새 릴리스 필요), 외부 호스트명 단위 제한(NetworkPolicy 한계)

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| N1 공통 변환 | `cmp_providers.network_policy.base_policies`(기본 차단 `cmp-default-deny`, DNS `cmp-allow-dns`, 리소스 간 의도마다 송신·수신 정책)와 `entry`(앱 포트 수신)를 샌드박스·운영이 함께 쓴다. 샌드박스 `build_manifests`는 이 함수 뒤에 프록시 규칙만 붙이고 출력은 바이트 단위로 같다. 이름은 기존 해시 규칙(`cmp-allow-<sha256 10자>`) 그대로 | 샌드박스 검증과 운영 정책이 같은 규칙에서 나와야 샌드박스 통과가 운영 의도와 맞는다. 샌드박스 동작은 바꾸지 않는다 |
| N2 앱 → DB | 분석 의도와 별개로 P0 토폴로지의 앱 → DB(TCP 5432) 간선을 항상 추가한다(`with_connections`, 중복 제거) | 플랫폼이 직접 앱을 대상 DB에 연결한다. 분석이 원본에서 간선을 못 봐도 운영 앱이 DB에 못 붙으면 안 된다 |
| N3 외부 진입(D-15) | 인터넷 → 앱은 `network_intent`로 표현하지 않는다. 플랫폼이 만드는 노출(AWS ALB `target-type: ip`, GKE Ingress NEG, K3s Traefik·Agent 터널)에서 파생해, 앱 포트 수신을 **출발지 제한 없이 포트로만** 연다. 다른 포트와 DB는 닫힌다 | 앱 포트는 어차피 공개 LB로 노출된다. LB 출발지는 Provider마다 다르고(ALB ENI = VPC CIDR, GFE 대역, Traefik Pod) 09·11·Agent의 API 서버 프록시 헬스 확인도 같은 포트를 쓴다. 출발지 제한은 보호 효과 없이 깨지기 쉽다. 분석 `external_entries`를 노출 설정에 반영하는 일은 32(#156)이다 |
| N4 외부 호스트 출구 | 운영에는 출구 프록시가 없다. `external_host` 의도가 있는 리소스에만 그 의도 포트로 `0.0.0.0/0`(사설 대역 `10/8`·`172.16/12`·`192.168/16` 제외) 송신을 연다. 의도가 없으면 정책도 없다 | NetworkPolicy는 호스트명을 모른다. 호스트 IP는 바뀌어 `ipBlock`으로 고정할 수 없다. 사설 대역 제외로 클러스터·VPC·터널 우회를 막는다(샌드박스 프록시와 같은 제외). 호스트명 단위 제한은 하지 않음을 Preview에 적는다. 운영 출구 프록시·FQDN 정책(Cilium 등)은 후속 |
| N5 플랫폼 운영 경로 | 의도 밖이지만 실행 쪽에 필요한 경로를 `applied_defaults`로 보이고 정책으로 둔다: DB → DB 5432 수신(09·11 Service 경유 자기 점검), DB → TCP 5432 송신(Data Migration 복제 연결), ON_PREM은 Agent Pod(`app.kubernetes.io/name: cmp-onprem-agent`, 모든 Namespace) → DB 5432(17 터널), Network Preflight 점검 Pod 송신 전체(MNP-01) | 정책을 켜면 이 경로가 조용히 끊긴다. 복제 실행 방식은 Data Migration 몫이라 포트만 열어 두고 세부는 정하지 않는다 |
| N6 산출물·Digest | 정책은 Plan 산출물 `network_policies`(kind `MANIFEST`, `{"policies": [...]}` canonical JSON)로 manifest에 고정한다. 실행 쪽은 승인 바이트를 그대로 적용한다. 입력 Digest에 `network_policy: "network-policy-v1"`를 넣는다(ON_PREM 18 Digest, AWS/GCP 22 워크로드 Digest. 인프라 Digest는 그대로) | Values에 넣으면 Chart를 바꿔야 하고 Agent가 이미 받는 Chart·Values 경로와 섞인다. 별도 산출물이면 manifest Digest로 승인되고 Agent는 job의 참조로만 받는다. Digest 표식이 없으면 티켓 26 이전 Revision이 정책 없이 재사용된다 |
| N7 Preview | `changes`에 NETWORK `CREATE` 줄: 기본 차단, 의도 간선, 외부 의도(호스트명 미강제 표시), 외부 진입. `applied_defaults`에 DNS와 N5 경로 | 승인자가 열리는 경로와 닫히는 경로를 이름·포트로 본다 |
| N8 적용 순서·소유 | Namespace 준비 직후, Secret·Helm 전에 적용한다. 정책은 `app.kubernetes.io/managed-by: cmp-provisioning`·`cmp.dev/managed: "true"`를 둘 다 단다(09·11과 Agent의 기존 Secret 소유 라벨). 같은 이름의 관리 라벨 없는 정책이 있으면 아무것도 바꾸기 전에 `NETWORK_POLICY_CONFLICT`. 적용 뒤 승인 집합에 없는 관리 정책은 지운다. 산출물이 없는 이전 Plan은 Namespace의 정책을 건드리지 않는다 | Pod가 처음부터 승인 규칙 아래에서 뜬다. 사용자 정책은 덮어쓰지도 지우지도 않는다. 의도에서 빠진 간선이 남지 않는다 |
| N9 강제 확인 | AWS/GCP Activity는 적용 전에 강제 엔진을 읽는다. EKS: `kube-system/aws-node`의 `aws-eks-nodeagent`가 `--enable-network-policy=true`. GKE: `kube-system/anetd` DaemonSet(Dataplane V2). 없으면 `NETWORK_POLICY_NOT_ENFORCED`로 실패하고 Helm을 하지 않는다. 결과 refs: `network_policy_enforcement`(`aws-vpc-cni`·`gke-dataplane-v2`), `network_policies`(이름 목록). ON_PREM은 K3s 내장 kube-router로 강제되며 k3d로 실제 확인했다 | 강제되지 않는 정책은 보호한다는 착각만 준다. 이 티켓 전 Terraform으로 만든 클러스터는 Terraform 재적용(EKS) 또는 재생성(GKE)이 필요하다 |
| N10 EKS 강제 | `aws_eks_addon "vpc_cni"`(관리형 애드온, `resolve_conflicts_on_create/update = "OVERWRITE"`, `configuration_values = {enableNetworkPolicy = "true"}`). 05 Preview는 `aws_eks_addon` 이름을 "EKS 애드온(EBS CSI·VPC CNI)"으로 바꾸고 기본값 줄을 더한다 | 기본 설치된 자체 관리 VPC CNI를 관리형으로 넘겨받아 설정한다. 기존 클러스터에도 in-place 생성이라 05의 신규 Plan 규칙(update·delete 거절)과 맞는다 |
| N11 GKE 강제 | `datapath_provider = "ADVANCED_DATAPATH"`(Dataplane V2). `network_policy` 블록(Calico)은 쓰지 않는다. 06 Preview 클러스터 설명에 표시 | Dataplane V2는 GKE 권장 경로로 NetworkPolicy를 별도 애드온 없이 강제하고 Autopilot 기본이다. Calico 방식은 애드온·노드 재생성이 필요하고 신규 기능이 없다. 값 변경은 클러스터 교체라 **신규 클러스터만** 대상이다(06의 신규 Plan 규칙이 기존 클러스터 교체를 거절) |
| N13 템플릿 버전 | AWS `aws-eks-v2` → `aws-eks-v3`, GCP `gcp-gke-v2` → `gcp-gke-v3`(05·06 Preview 확인값, 18 입력 Digest) | 템플릿이 바뀌었으므로 기존 등록이 티켓 26 이전 인프라 Revision(강제 없음)을 재사용하지 않고 다시 Plan한다. 08·10 Apply의 템플릿 지문 확인도 이전 Plan 적용을 막는다 |
| N12 Agent | job에 선택 필드 `network_policies: ArtifactRef`(값이 없으면 직렬화에서 빠져 이전 job과 같다). Agent는 산출물을 받아 Digest 확인 → 형식·Namespace·관리 라벨 확인 → 충돌 확인 → 적용 → 제거, READY 확인에서 승인 정책 존재를 본다. Agent RBAC에 `networkpolicies` 권한 추가 | 이전 Agent는 `extra=forbid`로 이 job을 `UNSUPPORTED_JOB`으로 거절한다(조용히 정책 없이 배포하지 않음). 새 Agent 이미지·Chart 릴리스가 필요하다(사용자 승인) |

## 구현 요약
- 공통: `cmp_providers.network_policy`(`base_policies`·`entry`·`production_policies`·`with_connections`·`describe`·강제 판정 함수). 샌드박스 `sandbox_network.build_manifests`가 공통 함수 사용
- 계약: `ArtifactRole` `network_policies`(`MANIFEST`), `ProductionRolloutJob.network_policies`(선택), OpenAPI 재생성(Agent job 스키마에 필드 1개)
- 게시: `plan_publish.network_policies`·`NetworkRendering`(산출물·Preview 줄), ON_PREM·워크로드 Plan 산출물 추가, Digest 표식
- 실행: `ApprovedWorkloads._network_policies`(산출물 검증)·`_apply_network_policies`·`policy_enforcement` 훅, `KubectlSession.get_object`·`network_policies`·`delete_network_policy`. Agent `RolloutRunner._network_policies`·`_apply_network_policies`, `build_rollout_job` 참조 전달
- 인프라: `infra/terraform/aws/controllers.tf` VPC CNI 애드온, `infra/terraform/gcp/main.tf` Dataplane V2(템플릿 `aws-eks-v3`·`gcp-gke-v3`), Agent Chart RBAC

## 검증 결과 (2026-11-06)
| 검사 | 결과 |
| --- | --- |
| `tests/test_network_policy.py` (평가기 세 대상·게시·09·11·Agent·CLI) | 23 통과 |
| `tests/sandbox` (샌드박스 회귀, 리팩터링 전후 출력 바이트 비교) | 235 통과, 출력 동일 |
| `make check` (ruff + 전체 pytest) | 통과 |
| `make openapi` | `ProductionRolloutJob.network_policies` 추가만 |
| `alembic check` | DB 변경 없음 |
| `terraform fmt -check` / `init -backend=false` / `validate` (AWS·GCP, Terraform 1.16.4 컨테이너) | 통과 |
| k3d 실제 강제 (`k3d-cmp-t26`, k3s v1.31.5+k3s1, kube-router) | 정책 전 9개 경로 모두 열림 → 정책 후 허용 5(앱 → DB, 다른 Namespace → 앱 포트, Traefik Ingress → 앱, API 서버 프록시 → 앱, DB 자기 Service), 차단 4(다른 Pod → DB, 다른 Namespace → DB, 앱 → 다른 Pod, 앱 → Kubernetes API 사설 IP). 이전 관리 정책 제거, 관리 라벨 없는 같은 이름 → `NETWORK_POLICY_CONFLICT`(그 Namespace 변경 없음). 클러스터 삭제, 기본 kubeconfig 변경 없음 |

## 남은 일
- EKS·GKE 실제 강제 확인: 승인된 Terraform 재적용(EKS 애드온 생성) 또는 신규 GKE 클러스터에서 09·11 실행 → `network_policy_enforcement` refs와 허용·차단 경로 확인(클라우드 비용·승인 필요)
- Agent 새 이미지·Chart 릴리스(사용자 승인): 이전 Agent는 정책이 든 job을 `UNSUPPORTED_JOB`으로 거절한다
- 이 티켓 전에 만든 GKE 클러스터는 Dataplane V2가 아니라 `NETWORK_POLICY_NOT_ENFORCED`로 워크로드 배포가 멈춘다. 재생성 필요
- 외부 호스트명 단위 출구 제한(운영 출구 프록시 또는 FQDN 정책)은 후속 결정
