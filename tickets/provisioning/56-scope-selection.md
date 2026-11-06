# Ticket 56 — 이전 범위 선택 API (EKS Namespace·워크로드 목록과 범위 후보)

## 상태
IN_PROGRESS (로컬 구현·검증·실제 원본 읽기 확인 완료, 리뷰·플랫폼 서버에서 원본 역할로 호출 확인 대기) — [Issue #219](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/219). 2026-11-06 조율 세션 지시로 착수. 담당 민서.

## 배경
웹의 이전 범위 선택(리전 → EKS 클러스터 → Namespace → 워크로드 → 후보 확정)은 웹 BFF 목업(`mockScope`)이 답했다. 리전 → 클러스터만 `GET /connections/{id}/eks-clusters`(23)가 있었다. 고객 AWS·EKS는 플랫폼만 AssumeRole로 읽으므로(ADR-005) 나머지 단계도 플랫폼 API에 둔다. ADR-005가 후속으로 남긴 "원본 클러스터 Kubernetes API 접근"을 이 티켓이 채운다.

## 관련 요구사항
PROV-021(연결·권한 경계). 계약: [shared-contracts.md "이전 범위 선택 (56)"](../../specs/provisioning/shared-contracts.md), 웹 `src/lib/connections/types.ts`(ScopeSelection·ScopeCandidate·ScopeAnalysis·ScopeConfirmation).

## 선행 작업
Blocked by: 23(연결), 46(연결 자격 증명). 실제 호출은 원본 클러스터의 접근 항목(아래 템플릿)이 필요하다.

## 작업 범위
- 포함
  - `GET /connections/{id}/eks-clusters/{cluster}/namespaces?region=`
  - `GET /connections/{id}/eks-clusters/{cluster}/namespaces/{namespace}/workloads?region=`
  - `POST /connections/{id}/scope-analyses`, `POST /scope-analyses/{analysis_id}/confirm`
  - 업무 DB `scope_analyses`·`scope_confirmations`(alembic `20261106_03`)
  - 클러스터별 접근 템플릿 `infra/connections/eks-view-access.yaml`
- 제외
  - 분석(`cmp_scanners`, analysis pipeline)·worker 변경. 확정 범위를 분석 입력에 잇는 일은 후속(분석 담당 협의)
  - `POST /migrations`의 `scope_id`(선택 항목) — 시작 계약(`cmp_contracts`)이 worker와 공유돼 이번에는 넣지 않았다(후속)
  - 원본 역할 템플릿(`source-role.yaml`) 변경, 템플릿 업로드·AWS 변경

## 착수 결정 (2026-11-06, Issue #219 표)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| S1 | EKS 쿠버네티스 API 권한 | 원본 역할 템플릿은 그대로 두고, 클러스터별 접근 템플릿(`AWS::EKS::AccessEntry` + `AmazonEKSViewPolicy`, 클러스터 범위)을 따로 둔다. 권한이 없으면 409 `EKS_ACCESS_DENIED` | 역할 템플릿은 계정 단위, 접근 항목은 클러스터 단위라 사용자가 고른 클러스터에만 준다 |
| S2 | 호출 방식 | 맡은 역할 자격 증명으로 EKS 토큰(STS `GetCallerIdentity` 서명 URL, `x-k8s-aws-id` 헤더, `k8s-aws-v1.` 접두, 60초)을 만들고 DescribeCluster의 endpoint·CA로 HTTPS GET만 한다. kubectl 없음. 401·403 → `EKS_ACCESS_DENIED`, 그 밖 실패 → `EKS_API_FAILED` | `eks:DescribeCluster`는 원본 역할에 이미 있다. 쿠버네티스 클라이언트는 의존성 주입(`get_kube_factory`) |
| S3 | 비밀값 | Secret·ConfigMap 객체를 읽지 않는다. Pod 정의의 참조 이름·키와 환경 변수 이름만 쓴다. `env[].value`는 읽지 않는다. 응답·로그·예외·DB에 값이 없다 | ADR-005 비밀값 규칙 |
| S4 | 후보 근거 | 실제로 읽은 것만: Ingress(ALB 클래스·`alb.ingress.kubernetes.io/*`, 상태 hostname이 `DescribeLoadBalancers`의 DNSName과 같으면 ARN), ServiceAccount `eks.amazonaws.com/role-arn`(IAM_ROLE, 그 역할 정책 문장의 리소스 ARN → S3·SQS 등), env·envFrom의 ConfigMap·Secret 이름과 키, 같은 Namespace의 PostgreSQL StatefulSet(이미지로 판별). 근거 없는 후보는 `suggested=false` | 웹 목업의 CLOUDTRAIL·CODE 근거는 이 API가 읽지 않으므로 내지 않는다 |
| S5 | ALB 찾기 | 로드밸런서 컨트롤러 태그(`elbv2.k8s.aws/cluster`, `ingress.k8s.aws/stack`) 대신 Ingress 상태 hostname ↔ `DescribeLoadBalancers` DNSName으로 찾는다 | 태그 조회(`DescribeTags`)는 원본 역할에 없고 S1에 따라 역할을 바꾸지 않는다. 같은 ALB를 가리키는 실제 값이다 |
| S6 | PostgreSQL 근거 | 이미지 저장소 마지막 이름이 `postgres`·`postgresql`·`postgis`로 시작하면 PostgreSQL StatefulSet. 워크로드와 같은 Secret·ConfigMap을 참조하거나, 워크로드를 고르는 NetworkPolicy의 egress가 그 Pod를 고르면 `suggested=true`, 아니면 같은 Namespace 근거만으로 `false` | DB 주소는 Secret 값이라 읽지 않는다 |
| S7 | IAM 정책 | 인라인·연결 정책의 `Allow` 문장에서 `*`가 없는 ARN만 후보로 만든다. S3 객체 ARN(`bucket/*`)은 버킷으로 접는다. IAM 조회 실패는 분석을 멈추지 않고 그 근거만 뺀다 | 와일드카드는 특정 리소스가 아니다 |
| S8 | 시스템 Namespace | `kube-*`, `amazon-*`, `aws-*`, `*-system`을 목록에서 빼고, 하위 경로에서는 `NAMESPACE_NOT_FOUND` | `default`는 앱이 있을 수 있어 남긴다 |
| S9 | 오류 | `CONNECTION_NOT_FOUND` 404, `SOURCE_CONNECTION_REQUIRED`·`CONNECTION_NOT_READY`·`EKS_ACCESS_DENIED`·`EKS_API_FAILED`·`ASSUME_ROLE_*` 409, `CLUSTER_NOT_FOUND`·`NAMESPACE_NOT_FOUND`·`WORKLOAD_NOT_FOUND`·`ANALYSIS_NOT_FOUND` 404, `INVALID_SCOPE` 400. detail은 코드만 | 23 C10 규칙 |
| S10 | 저장 | 분석 결과(선택·후보)와 확정 범위를 업무 DB에 저장한다. 확정은 여러 번 할 수 있고 매번 새 `scope_id` | 마지막 확정이 유효한지는 소비자(후속)가 정한다 |
| S11 | 시각 | `confirmed_at`은 웹 타입과 같게 ISO-8601 UTC 문자열(DB는 epoch 초) | 웹 `ScopeConfirmation.confirmed_at: string`. 23 C12(epoch 초)와 다르다 |
| S12 | 분석·worker | 수정하지 않는다 | 사용자 결정 |
| S13 | 웹 차이 | 워크로드 목록은 `{kind, name}`(웹은 `string[]`). 분석 요청은 선택 필드 `workload_kind`를 더 받는다 | 같은 이름의 Deployment·StatefulSet 구분 |

## 접근 템플릿
`infra/connections/eks-view-access.yaml`. 매개변수 `ClusterName`, `PrincipalArn`(원본 연결의 Role ARN). 리소스는 `AWS::EKS::AccessEntry`(STANDARD) 하나, 정책 `arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy`, 범위 `cluster`. 클러스터 인증 모드가 `API` 또는 `API_AND_CONFIG_MAP`이어야 한다. 호스팅은 C9 규칙대로 `connections/v2/eks-view-access.yaml`(플랫폼 S3 버킷, 공개 읽기). 업로드와 빠른 생성 링크 노출은 후속이며 승인 대상이다.

## 검증 기록
- `tests/test_scope_selection.py`: 가짜 STS·EKS·ELBv2·IAM(Stubber)과 가짜 쿠버네티스 API
  - 운영자 키 필수(4개 경로), 시스템 Namespace 제외, Deployment·StatefulSet 목록
  - EKS 토큰 형식(`k8s-aws-v1.`, `x-k8s-aws-id` 서명 헤더), DescribeCluster endpoint·CA 전달
  - `EKS_ACCESS_DENIED`, `CLUSTER_NOT_FOUND`·`NAMESPACE_NOT_FOUND`·`WORKLOAD_NOT_FOUND`·`ANALYSIS_NOT_FOUND`, `SOURCE_CONNECTION_REQUIRED`·`CONNECTION_NOT_READY`, `INVALID_SCOPE`
  - 후보: ALB(ARN), IAM_ROLE → S3·SQS, Secret·ConfigMap 참조, PostgreSQL(NetworkPolicy·공유 Secret 근거, 근거 없음 `suggested=false`)
  - 비밀값 비노출: Secret·ConfigMap 경로 미조회, `env[].value`가 응답·DB·로그에 없음
- 접근 템플릿 YAML 구조 검사(`tests/test_connection_templates.py`)
- `make check`, alembic(SQLite) upgrade·check, `make openapi`

- 실제 원본 읽기 확인(2026-11-06, 읽기 전용, 쓰기 없음): `cmp-demo-source`(ap-northeast-2, 계정 714066696240, 인증 모드 `API_AND_CONFIG_MAP`, 공개 endpoint)
  - 원본 연결 역할 `cmp-source-915ac0a11f34-ConnectionRole-…`에 STANDARD 접근 항목 + `AmazonEKSViewPolicy`(cluster 범위)가 이미 있다(2026-11-06 15:06 KST 생성, `list-associated-access-policies`로 확인)
  - 이 티켓 코드(`eks_token` → DescribeCluster CA → `EksKubeApi` GET → `analyze_scope`, 메모리 SQLite)를 운영자 SSO 주체로 실행: Namespace `[chat-source, default]`, 워크로드 `Deployment chat-app`·`StatefulSet chat-db`, 후보 4개 — ALB(`k8s-chatsour-chatapp-0ac465901d`, DNSName 일치로 ARN), Secret `chat-secrets`(`DATABASE_URL` 키), ConfigMap `chat-app-config`(envFrom), PostgreSQL `chat-db`(`postgres:16`, NetworkPolicy `chat-app-egress` + 공유 Secret 근거, `suggested=true`). chat-app에는 IRSA가 없어 IAM 후보가 없다

## 미검증
- 원본 연결 역할 자체로의 호출: 신뢰 대상이 플랫폼 주체뿐이라 이 작업 환경에서 그 역할을 맡을 수 없다. 위 확인은 더 넓은 권한(SSO 관리자)으로 같은 코드 경로를 돈 것이다. `AmazonEKSViewPolicy`가 읽는 종류(namespaces, deployments, statefulsets, services, serviceaccounts, ingresses, networkpolicies)를 모두 허용하는지는 플랫폼 서버에서 API를 호출해 확인해야 한다
- 웹 BFF 연결(웹 저장소, 범위 밖)
