# Ticket 09 — AWS EKS에 DB·앱 배포 및 연결 확인

## 상태
DONE — [Issue #37](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/37); 08이 만든 실제 EKS `cmp-target-aws`에서 Activity 실검증 완료. 08 병합 후 승인 Workflow 연결 확인은 아래 미검증 항목

## 목적
준비된 EKS에 PostgreSQL과 앱을 배포하고 데이터 이전 전 준비 상태를 검증한다.

## 관련 요구사항
PROV-010, PROV-011, PROV-012, PROV-013, PROV-018

## 선행 작업
Blocked by: 02, 03, 08

## 작업 범위
- 포함: Secret 주입, DB·앱 Helm Release, EBS PVC·Service·ALB Ingress와 헬스/DB 연결 확인을 구현한다.
- 제외: 데이터 이전·트래픽 가중치 변경

## 수용 기준
- [x] DB PVC Bound·접속과 앱 Pod Ready·헬스를 별도로 확인한다.
- [x] ALB Ingress 주소와 실제 외부 접근 경로를 확인한다.
- [x] 이미지 Pull·Secret 참조·PVC 오류가 있으면 `READY`를 반환하지 않는다.

## 테스트 전략
- 제안 Test Seam: EKS 배포 요청 → Kubernetes/Ingress/앱 준비 결과. 데이터 정합성은 확인하지 못한다.
- 실행: Helm 렌더/Release 재조회와 격리 EKS 통합 검증; `make check` 및 `helm lint`.

## 완료 시 확인할 동작
EKS에서 실제 DB·앱 준비와 Endpoint를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: ECR Pull 권한, TLS/도메인 및 Production 기능 테스트 범위를 결정한다. → W3·W4·W10으로 결정.
- 구현 중 변경: 2026-11-04 착수 결정 W1~W11 참조(ECR Pull·TLS 없음·Ingress·Controller/StorageClass·Secret·READY 판정).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰; PR 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## P0 정책 갱신 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 신규 AWS 네트워크·EKS(08 산출)에만 배포한다. DB와 App은 별도 Chart/Release이며 운영 비밀번호를 Values에 싣지 않는다. 실행은 특정 Plan Revision의 승인·Digest 재확인 이후에만 한다.

## 착수 결정 (2026-11-04)

사용자 지시: 기획 문서의 최신 결정을 우선 적용하고, 없는 항목은 구현자가 최선안을 정해 여기에 기록한다. 실행 분기는 08의 E1을 따른다(`claim_approved_execution` → `execute_aws_workloads` → `complete_approved_execution`).

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| W1 Activity 계약 | 새 모듈 `cmp_worker.activities.aws_workloads`의 `execute_aws_workloads`(`provisioning` queue). 입력 `{approval_id, plan_id, revision, provider, scope, manifest_digest}`, 출력 `{status: SUCCEEDED/FAILED/UNKNOWN, code: UPPER_SNAKE, resource_refs: {str: str}}`. 비밀값·kubeconfig·명령 출력은 넣지 않는다. 실행 시작 시 업무 DB에서 Plan Revision·승인 기록을 다시 읽어 APPROVE·최신 Revision·`manifest_digest` 재계산·`provider=AWS`·scope(`WORKLOADS`/`FULL`)를 확인하고 하나라도 다르면 클러스터를 건드리지 않고 FAILED를 반환한다. 승인 Workflow·`cmp_api/approvals.py`는 수정하지 않는다. | 08 E1 계약. claim 이후에도 Activity 단독 호출 방어(plan.md §10). |
| W2 산출물 검증 | `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`(05 출력 디렉터리와 같은 Plan 폴더, k3s_agent와 같은 내용 주소)에서 `database_chart`·`database_values`·`application_chart`·`application_values`·`bindings` 바이트를 읽어 `verify_artifact_bytes`로 크기·SHA-256을 확인한다. Chart는 `helm package` tgz, Values는 `cmp-json-v1` JSON, bindings는 `DynamicBinding` 목록 JSON이다. DB Values는 `PostgresChartValues`(PRODUCTION·AWS), App Values는 DB 슬롯이 null인 `ApplicationValues` 템플릿이며 `manifest.image_uri`와 앱 이미지가 같아야 한다(PROV-010 동일 Digest). DB host(`<db_release>.<namespace>.svc`)·DB Secret 이름을 실행 시 해석해 `resolve_application`/`verify_application`으로 승인 슬롯 외 변경을 거절한다. | ADR-002 #3, CP-01 바인딩 규칙. S3 저장소 연동은 후속(08 E3과 동일). |
| W3 ECR Pull | 이미지는 같은 계정 ECR Digest를 그대로 사용하고 private Node는 관리형 Node 역할의 `AmazonEC2ContainerRegistryReadOnly` + NAT 경로로 Pull한다(imagePullSecret 없음, `registry_auth_ref`는 기존 Chart처럼 거절). Pull 실패(`ErrImagePull`/`ImagePullBackOff`)는 `IMAGE_PULL_FAILED`로 READY 금지. 교차 계정·외부 Registry 인증은 후속. | plan.md §9 "Registry Pull 인증 검증", 소스 데모와 같은 계정 ECR. |
| W4 TLS·도메인 | P0는 도메인·인증서 없이 ALB HTTP:80만 공개한다. 외부 경로는 Ingress `status.loadBalancer` 호스트명 `http://<alb-dns>`다. HTTPS·Route 53·ACM은 Traffic Migration/후속 결정. | spec.md 미정 사항(도메인·TLS), 소스 데모 Ingress도 HTTP:80. |
| W5 Ingress | 앱 Chart 계약을 바꾸지 않고, 승인된 App Values(Release·Namespace·Port·Health Path)에서 결정적으로 만든 고정 템플릿 Ingress(`ingressClassName: alb`, internet-facing, target-type ip, healthcheck-path=health_path)를 `kubectl apply --server-side`로 적용한다. Preview의 `INGRESS` 변경과 대응한다. | ApplicationValues/Chart 스키마는 03·04B·11·12가 공유하므로 변경 회피. Ingress를 Chart로 옮기는 것은 Planning 연결 시 후속. |
| W6 Controller·StorageClass | 클러스터 선행 단계에서 (a) DB Values의 `storage.class_name` StorageClass를 `ebs.csi.aws.com`·gp3·암호화·`WaitForFirstConsumer`로 없으면 생성하고, 다른 provisioner로 이미 있으면 `STORAGE_CLASS_MISMATCH`; (b) AWS Load Balancer Controller를 공식 eks-charts의 고정 버전 Chart(tgz SHA-256 고정 확인)로 `kube-system/aws-load-balancer-controller` ServiceAccount에 08의 IRSA 역할 ARN 주석을 달아 설치/업그레이드한다. EBS CSI Add-on 자체는 08 Terraform 소유. | 08 E6(IRSA·Add-on은 08, Helm 설치·StorageClass는 09). |
| W7 Secret | DB 비밀번호는 Worker가 `secrets.token_urlsafe`로 만들고 Kubernetes Secret을 stdin JSON으로 생성한다(명령 인자·로그·결과·Temporal History에 원문 없음). 이미 있으면 재생성·회전하지 않는다(PVC 초기화 비밀번호 보존). 앱의 `USER_SECRET`·`CONFIG_MAP` 참조는 사용자 업로드/운영 준비 대상이라 09가 만들지 않으며, Helm 설치 전에 이름·키 존재를 확인하고 없으면 `USER_SECRET_MISSING`/`CONFIG_MAP_MISSING`으로 실패한다. | PROV-012, 공통 계약 "Secret 원문은 렌더러에 넣지 않는다". 값 확인은 키 이름만. |
| W8 멱등성 | Namespace·Secret·StorageClass는 조회 후 생성. Helm Release는 `helm status`로 먼저 조회한다: 없음 → install, deployed + 같은 Values·Chart 버전 → 건너뜀, deployed + 다름 → upgrade, `pending-*` → 변경 없이 `UNKNOWN`(`HELM_RELEASE_PENDING`), failed → `FAILED`(`HELM_RELEASE_FAILED`). Helm `--atomic`·자동 rollback/uninstall·재시도는 하지 않는다. | PROV-013 "재시도 시 중복 생성 금지", plan.md "Helm 응답 유실 후 Release 재조회". |
| W9 READY 판정 | 순서: DB Release → PVC Bound → StatefulSet Ready → **인증된 DB 접속**(DB Pod 안에서 컨테이너 env의 비밀번호로 Service DNS에 `psql`, PostgreSQL 16 버전 확인, 복제 권한 여부 기록) → App Release → Deployment Ready → 클러스터 내부 헬스(API server Service proxy로 `health_path` 200) → Ingress ALB 주소 할당 → 외부 `http://<alb>/<health_path>` 200. 하나라도 실패/시간 초과면 SUCCEEDED가 아니다(`PVC_NOT_BOUND`, `IMAGE_PULL_FAILED`, `SECRET_REFERENCE_INVALID`, `DB_CONNECT_FAILED`, `POD_NOT_READY`, `HEALTH_CHECK_FAILED`, `ALB_ADDRESS_PENDING`, `ALB_UNREACHABLE` 등). 성공 code는 `WORKLOADS_READY`. | PROV-018, testing-strategy 검증 행렬. |
| W10 범위 밖 | Production 기능 테스트(데이터 변경 포함)는 실행하지 않는다. 데이터 이전·CDC·트래픽 가중치, HTTPS/도메인, 실패 시 자동 정리, ProvisioningResult 저장·인계(13/14)는 범위 밖. | 티켓 제외 범위, plan.md §11 "Production 데이터 변경 테스트는 결정 필요". |
| W11 클러스터 식별 | WORKLOADS manifest에는 클러스터 이름이 없으므로 08이 기록한 같은 대상(environment)의 인프라 실행 `resource_refs`(클러스터 이름·VPC ID·LB Controller IRSA ARN)를 `TargetResolver` 포트로 읽는다. kubeconfig는 `aws eks update-kubeconfig`로 0600 임시 파일에 만들고 실행 후 삭제한다. 계정은 STS로 manifest `account_id`와 비교한다. | 08과 조율(세부 키 이름은 08 반영 후 확정). |

### 합의한 Test Seam

- Seam: `temporalio.testing.ActivityEnvironment`로 실제 `execute_aws_workloads`를 호출한다. 업무 DB는 SQLite(07 모델로 게시·승인 기록), 산출물은 임시 `PROVISIONING_ARTIFACT_ROOT`의 실제 Chart tgz·Values·bindings 바이트, 클러스터·Helm·AWS·HTTP는 상태를 가진 가짜 런타임 포트로 바꾼다.
- 잡아내는 것: 승인/Revision/Digest/Provider/scope 불일치 시 클러스터 무변경, 산출물 바이트 변조·누락, 승인 슬롯 외 Values 변경·이미지 불일치, 기존 Release 재조회에 따른 install/skip/upgrade/pending/failed 분기, Secret 원문이 결과·로그·런타임 인자에 없음, 기존 DB Secret 미회전, 사용자 Secret/ConfigMap 누락, PVC Pending·이미지 Pull·Secret 참조 오류·DB 접속·헬스·ALB 실패 시 READY 금지, 성공 시 참조만 반환.
- 놓치는 것: 실제 EKS IAM/IRSA·EBS CSI·ALB 조정 시간, 실제 kubectl/Helm CLI 인자 호환성, 승인 Workflow 연결(08 반영 후). 이는 아래 실제 EKS 검증과 08 병합 후 Temporal 테스트로 보완한다.

## 구현 (2026-11-04)

| 경계 | 위치 |
| --- | --- |
| Activity 오케스트레이션·승인/산출물 재확인·READY 판정·`ExecutionRefsResolver` | `apps/worker/src/cmp_worker/activities/aws_workloads.py` |
| kubectl/Helm/AWS CLI 어댑터(kubeconfig 0700 임시 디렉터리, Secret stdin `create`, LB Controller Chart 3.6.2 SHA-256 고정), Worker 조립 | `apps/worker/src/cmp_worker/activities/aws_workloads_cli.py` |
| 등록 | `apps/worker/src/cmp_worker/workers/provisioning.py` 한 줄 |
| 테스트 | `tests/test_aws_workloads.py` |

Worker 환경변수는 08과 같은 `PROVISIONING_ARTIFACT_ROOT`, `PROVISIONING_AWS_PROFILE`에 더해 선택적으로 `HELM_BIN`, `KUBECTL_BIN`, `AWS_BIN`을 읽는다. 08의 `config.py`·`.env.example` 변경과 충돌하지 않도록 이 티켓은 그 파일을 수정하지 않았다.

08 조율 결과(08 세션, 2026-11-04): FULL은 Workflow가 이전 단계 refs를 `{**claim, "resource_refs": refs}`로 넘기고(08 커밋 73f81b5), WORKLOADS 단독은 같은 대상(environment·계정·리전·State)의 최신 SUCCEEDED INFRASTRUCTURE/FULL 실행 refs를 쓴다. ref 키는 `eks_cluster_name`·`vpc_id`·`lb_controller_role_arn`. EBS CSI Add-on·IRSA는 08 Terraform, LB Controller IRSA는 `kube-system/aws-load-balancer-controller` SA 주석으로 연결한다. Node 역할은 `AmazonEC2ContainerRegistryPullOnly`, private subnet은 NAT 경유.

## 검증 기록 (2026-11-04)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_aws_workloads.py` (macOS, Python 3.11) | 37 passed, 1 skipped(08 스키마 필요한 DB resolver 테스트) |
| `make check` | 340 passed, 1 skipped |
| 임시 worktree에서 `feat/aws-infra-apply`(08) 병합 후 전체 `pytest` | 359 passed, skip 없음(DB resolver 테스트 통과). 충돌은 `tasks.md`와 Worker 등록 한 줄뿐 |
| 실제 EKS `cmp-target-aws`(08, ap-northeast-2, 714066696240, Node 2 Ready), Namespace `cmp-demo-target`, 실제 `CliRuntime` | 1차: `FAILED/LB_CONTROLLER_NOT_READY`(Helm v4에 `helm list -a` 없음 → 상태 필터 플래그로 수정·회귀 테스트). 2차: `SUCCEEDED/WORKLOADS_READY`, 재실행도 `WORKLOADS_READY` |
| 독립 확인(kubectl/helm/curl) | Helm Release 3개 모두 revision 1(재실행 시 재설치·업그레이드 없음), PVC `cmp-demo-db` Bound 10Gi `cmp-gp3`(ebs.csi.aws.com, gp3, Retain), DB·앱 Pod 1/1 Running, DB 인증 접속 PostgreSQL 16·복제 권한 true, 외부 `http://<ALB>/health` 200, LB Controller SA에 08 IRSA ARN, DB Secret은 키 이름만 확인·last-applied 주석 없음. 증거: [provisioning-09-eks.json](../../docs/verification/provisioning-09-eks.json) |
| 소스 EKS `cmp-demo-source` 읽기 전용 조회 | 데모 env 이름·이미지 Digest·ALB 주석·LB Controller 3.6.2 확인. 변경 없음, Secret 값 미조회(키 이름만) |

Chart 변경은 없어 `helm lint`/`helm template` 대상이 아니다(기존 렌더 테스트는 `make check`에 포함). HTTP 라우트 변경이 없어 `make openapi`는 해당 없음. Workflow 코드는 수정하지 않았고 승인 Workflow 연결 테스트는 08 병합 후 실행한다.

### 미검증·차단

- 승인 Workflow를 거친 실행이 아니다. 실검증은 임시 SQLite 업무 DB에 WORKLOADS Revision을 게시·승인하고 `ActivityEnvironment`로 실제 Activity를 호출했다. 클러스터 대상은 08 실행 refs를 같은 키로 전달했다.
- 실제 실패 주입(이미지 Pull·PVC Pending·Secret 참조 오류)은 가짜 런타임 테스트로만 확인했다. 실제 클러스터에서는 첫 시도의 LB Controller 실패가 코드로만 보고되고 이후 단계가 실행되지 않은 것만 관찰했다.
- 저장소 기준 Helm 3.17.3이 아니라 로컬 Helm v4.3.1로 검증했다. 상태 필터 플래그는 두 버전 모두 지원한다.
- 08 병합 후 `ProvisioningApprovalWorkflow` → `execute_aws_workloads` Temporal 테스트·`make smoke-temporal`.
- 사용자 `USER_SECRET`·`CONFIG_MAP`의 실제 생성 경로(업로드 `.env` 해석)는 Planning/업로드 통합 범위다. 09는 존재와 키만 확인한다.

실검증 자원(클러스터·LB Controller·`cmp-gp3`·`cmp-demo-target`의 DB/앱/ALB)은 지시에 따라 삭제하지 않았다. 사용자 Secret `cmp-demo-secrets`는 검증용으로 새로 만든 무작위 토큰이며 원본 데모 토큰을 복사하지 않았다. ALB는 인터넷 공개 HTTP라 비용·노출이 남으므로 정리 시점은 사용자가 결정한다.
