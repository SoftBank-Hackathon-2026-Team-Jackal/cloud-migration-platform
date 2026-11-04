# Ticket 11 — GCP GKE에 DB·앱 배포 및 연결 확인

## 상태
IN_PROGRESS — [Issue #43](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/43); 착수 결정 K1~K12

## 목적
준비된 GKE에 PostgreSQL과 앱을 배포하고 실제 GCP Ingress/LB 경로를 검증한다.

## 관련 요구사항
PROV-010, PROV-011, PROV-012, PROV-013, PROV-018

## 선행 작업
Blocked by: 02, 03, 10

## 작업 범위
- 포함: GKE용 Values, Persistent Disk PVC, Secret/Service, Helm Release, 앱·DB 접속과 GCP Ingress 상태를 구현한다.
- 제외: 데이터 이전과 트래픽 가중치 변경

## 수용 기준
- [ ] PVC Bound·DB 접속, 앱 Ready·헬스 및 GCP 외부 주소를 확인한다.
- [ ] AWS ALB Controller를 GKE 경로에 요구하지 않는다.
- [ ] 스토리지·이미지 Pull·Ingress 실패 때 `READY`를 반환하지 않는다.

## 테스트 전략
- 제안 Test Seam: GKE 배포 요청 → 클러스터/Ingress/앱 준비 결과. 데이터 이전 성공은 확인하지 못한다.
- 실행: GCP Values 렌더/Release 재조회와 격리 GKE 통합 검증; `helm lint`, `make check`.

## 완료 시 확인할 동작
GKE의 실제 DB·앱 준비와 Endpoint를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Registry Pull, TLS/도메인, Production 기능 테스트 범위를 정한다. → K4·K5·K11로 결정.
- 구현 중 변경: 2026-11-04 착수 결정 K1~K12 참조.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 착수 결정 (2026-11-04)

사용자 지시: 기획 문서의 최신 결정을 우선 적용하고, 없는 항목은 구현자가 최선안을 정해 여기에 기록한다. 실행 분기는 10의 G1을 따른다((GCP, WORKLOADS) → `execute_gcp_workloads`, (GCP, FULL) → `execute_gcp_plan` 다음 `execute_gcp_workloads`). 09 결정 W1~W11 중 Provider와 무관한 부분은 그대로 따른다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| K1 Activity 계약 | 새 모듈 `cmp_worker.activities.gcp_workloads`의 `execute_gcp_workloads`(`provisioning` queue). 입력·출력·실행 전 재확인은 09 W1과 같고 `provider=GCP`, DB/App Values의 `provider=GCP`를 요구한다. 승인 Workflow·`cmp_api/approvals.py`·`cmp_contracts/execution.py`·`infra/terraform/`은 수정하지 않는다(10 소유). | 08 E1·10 G1 |
| K2 09 재사용 | Provider와 무관한 오케스트레이션(승인·산출물 재확인, 승인 슬롯 바인딩, Helm Release 조회 분기, Pod/PVC 대기, DB Secret, 인증 DB 접속, 내부 헬스)을 `cmp_worker.activities.k8s_workloads`의 공통 기반 클래스로, kubectl/Helm 세션을 `k8s_workloads_cli`로 옮긴다. `aws_workloads`·`aws_workloads_cli`는 Provider 고유 부분(LB Controller·gp3·ALB·EKS 접속)만 남기고 기존 공개 이름을 유지한다. 09 테스트는 수정하지 않고 그대로 통과해야 한다. | 복사 시 두 경로의 READY 판정이 어긋날 위험. 09 공개 이름 유지로 회귀 방지. |
| K3 산출물 검증 | 09 W2와 같다(`PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`, 5개 역할, `verify_artifact_bytes`, 앱 이미지 = `manifest.image_uri`). | ADR-002 #3 |
| K4 이미지 Pull | 이미지는 승인된 ECR Digest URI를 그대로 쓴다(재태깅·미러 없음). 앱/DB 이미지 중 ECR URI가 있으면 Worker가 `PROVISIONING_AWS_PROFILE`(`cmp-aws-prod`)로 `aws ecr get-login-password`를 실행하고, 그 토큰으로 Namespace에 `kubernetes.io/dockerconfigjson` Secret `cmp-ecr-pull`을 stdin server-side apply로 만들거나 갱신한다. 두 Chart는 ServiceAccount를 지정하지 않으므로 Namespace `default` ServiceAccount의 `imagePullSecrets`에 그 Secret을 연결한다(Chart 계약 무변경). 토큰은 명령 인자·로그·결과·History에 남기지 않는다. ECR 레지스트리 계정이 STS 계정과 다르면 `ECR_ACCOUNT_MISMATCH`, 토큰·Secret 준비 실패는 `IMAGE_PULL_SECRET_FAILED`, Pod의 `ErrImagePull`/`ImagePullBackOff`는 `IMAGE_PULL_FAILED`(READY 금지). ECR 토큰은 12시간 유효하므로 재실행 시 갱신되며, 장기 자동 갱신(CronJob·Workload Identity Federation)이나 Artifact Registry 미러는 후속이다. ECR이 아닌 공개 이미지는 Secret 없이 Pull한다. | Artifact Registry 미러는 저장소·노드 SA IAM을 10의 Terraform에 추가해야 하고(노드 SA는 `roles/container.defaultNodeServiceAccount`만 보유, 10 G6), 이미지 URI가 승인 Values와 달라져 PROV-010 승인 슬롯 규칙과 충돌한다. |
| K5 TLS·도메인 | 09 W4와 같이 P0는 도메인·인증서 없이 HTTP:80만 공개한다. 외부 경로는 `http://<Ingress IP>`. | spec.md 미정 사항 |
| K6 Ingress | GKE 기본 GCE Ingress Controller(외부 Application Load Balancer)를 쓴다. 승인된 App Values에서 결정적으로 만든 고정 Ingress(`kubernetes.io/ingress.class: gce`, 경로 `/` Prefix → 앱 Service)를 server-side apply한다. Chart의 ClusterIP Service에는 별도 field manager로 `cloud.google.com/neg: {"ingress": true}` 주석만 추가해 컨테이너 네이티브 LB(NEG)를 명시한다. LB 헬스 체크는 Chart readinessProbe(`health_path`)에서 추론된다. 외부 주소는 `status.loadBalancer.ingress[0].ip`. AWS Load Balancer Controller·ALB 주석은 쓰지 않는다. | 원본 Ingress/ALB(L7)와 대응하고 Preview `INGRESS` 변경과 맞는다. Service `LoadBalancer`(L4)는 경로·HTTP 헬스가 없어 제외. GCE LB 프로그래밍은 수 분 걸리므로 주소·외부 헬스 대기 기본값을 15분으로 둔다. |
| K7 StorageClass | DB Values의 `storage.class_name`이 없으면 `pd.csi.storage.gke.io`·`type: pd-balanced`·`Retain`·`WaitForFirstConsumer`·확장 허용으로 생성하고, 다른 provisioner로 이미 있으면 `STORAGE_CLASS_MISMATCH`. PD CSI 드라이버는 GKE Standard 기본 제공(10 G5). | 리전 클러스터에서 WaitForFirstConsumer로 Pod 영역에 zonal PD를 만든다. Regional PD·영역 장애 대응은 후속. |
| K8 Secret | 09 W7과 같다(DB 비밀번호 Worker 생성·stdin `create`·기존 Secret 미회전, 사용자 `USER_SECRET`/`CONFIG_MAP`은 존재·키만 확인). | PROV-012 |
| K9 멱등성 | 09 W8과 같다(Release 조회 후 install/skip/upgrade, `pending-*` UNKNOWN, failed FAILED, atomic·rollback·재시도 없음). Helm 4의 `list -a` 제거에 대응한 상태 필터 플래그를 공통 세션에서 그대로 쓴다. | PROV-013 |
| K10 READY 판정 | 09 W9 순서에서 마지막 두 단계만 GCP 고유다: … → 내부 헬스 → Ingress IP 할당(`INGRESS_ADDRESS_PENDING`) → 외부 `http://<ip><health_path>` 200(`INGRESS_UNREACHABLE`). 성공 code `WORKLOADS_READY`. 결과 refs는 09와 같은 키(`db_host`·`app_external_endpoint` 등)에 `gke_cluster_name`·`gcp_project_id`·`ingress_ip`를 더한다. | PROV-018 |
| K11 범위 밖 | 09 W10과 같다. Production 기능 테스트, 데이터 이전·CDC·트래픽, HTTPS/도메인, 실패 시 자동 정리, ProvisioningResult 저장(13/14). | 티켓 제외 범위 |
| K12 클러스터 식별·접속 | `request["resource_refs"]`(FULL에서 10이 전달)의 `gcp_project_id`·`gke_cluster_name`·`gke_location`을 먼저 쓰고, 없으면 같은 대상(provider·environment·project·region·State)의 최신 SUCCEEDED INFRASTRUCTURE/FULL 실행 refs를 쓴다. `gcp_project_id` = manifest `account_id`(`PROJECT_MISMATCH`), `gke_location`은 manifest region 또는 그 영역(`REGION_MISMATCH`). Worker는 `PROVISIONING_GCLOUD_CONFIGURATION`(`cmp-gcp-prod`)의 gcloud 프로젝트가 manifest와 같은지 확인하고, `gcloud container clusters describe` 상태가 `RUNNING`이 아니면 `CLUSTER_NOT_RUNNING`. kubeconfig는 `gcloud container clusters get-credentials`로 0700 임시 디렉터리의 0600 파일에 만들고(`gke-gcloud-auth-plugin` 필요, `~/.kube/config` 미사용) 실행 후 삭제한다. | 10 G4 refs 키 |

### 합의한 Test Seam

- Seam: `temporalio.testing.ActivityEnvironment`로 실제 `execute_gcp_workloads`를 호출한다. 업무 DB는 SQLite(07 모델로 게시·승인), 산출물은 임시 `PROVISIONING_ARTIFACT_ROOT`의 실제 Chart tgz·Values·bindings 바이트, 클러스터·Helm·gcloud·AWS(ECR)·HTTP는 상태를 가진 가짜 런타임 포트로 바꾼다. CLI 어댑터는 기록용 러너로 인자·stdin을 검사한다.
- 잡아내는 것: 승인/Revision/Digest/Provider/scope·프로젝트·리전 불일치 시 클러스터 무변경, 클러스터 비RUNNING, 산출물 변조·누락, 승인 슬롯 외 Values 변경, Release install/skip/upgrade/pending/failed 분기, PD StorageClass 생성·불일치, ECR Pull Secret 생성·default SA 연결과 토큰 미노출(결과·로그·명령 인자), ECR 계정 불일치, Pull/PVC/Secret 참조/DB 접속/헬스/Ingress 주소·외부 접근 실패 시 READY 금지, AWS LB Controller 미호출, 성공 시 참조만 반환. 09 회귀(기존 테스트 무수정 통과).
- 놓치는 것: 실제 PD CSI 프로비저닝·GCE LB/NEG 조정 시간과 방화벽, 실제 ECR 토큰 Pull, gcloud/kubectl/Helm CLI 호환성, 승인 Workflow 연결(10 병합 후). 10이 만든 GKE의 `cmp-demo-target` 실검증과 10 병합 후 Temporal 테스트로 보완한다.
