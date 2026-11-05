# Provisioning 기능 요구사항

## 1. 기능 개요와 목적

Provisioning은 원본 AWS EKS·애플리케이션 분석, AI 코드·설정 변환, 이미지 빌드와 해당 Digest의 Sandbox 검증을 거쳐 만들어진 **공통 리소스 요구사항**을 대상 환경의 구성으로 변환한다. 사용자에게 생성·변경 계획을 Preview로 보여주고 **승인 후에만** 실제 대상 리소스를 생성·배포한다. 실행 결과와 연결 정보를 Data Migration 및 Traffic Migration에 제공한다.

새 [Planning 입력 계약 v2](input-contract-v2.md)는 요구사항을 고유 ID·종류를 가진 `resources[]`와 리소스 간 ID 참조로 표현한다. 초기 허용 종류는 APPLICATION·POSTGRESQL이며 미지원 종류와 PostgreSQL 확장은 명시적으로 거절한다. [공통 렌더 계약 v2](onprem-shared-deployment.md#v2-공통-렌더-입력반환)는 여러 앱·DB의 Chart 구성본을 만들며 실제 클라우드 실행 완료를 뜻하지 않는다.

이 문서는 기능이 충족해야 할 조건과 수용 기준을 정의한다. `ProvisioningSpec`과 `ProvisioningResult`는 여기서 **개념적인 입력·출력 계약의 이름**이며, 구체적인 Pydantic 필드·기본값, Terraform 모듈, Helm 차트, Temporal Activity 분할과 API 경로는 후속 설계에서 정한다. 제품 범위는 [제품 정책](../../docs/product/policies.md), 모듈 책임은 [도메인 경계](../../docs/architecture/domain-boundaries.md)를 따른다.

```text
AWS EKS·앱 분석 → AI 코드·설정 변환 → 이미지 빌드·Digest → 해당 Digest로 Sandbox 검증
  → 공통 ProvisioningSpec → Provider별 구성·Preview → 사용자 승인
  → 실제 인프라 구성·Kubernetes 배포 → 실제 환경 검증
  → ProvisioningResult → Data Migration / Traffic Migration
```

Provisioning의 완료는 **대상 인프라와 워크로드가 다음 이전 단계에 필요한 상태로 준비됨**을 뜻한다. PostgreSQL 데이터 이전이나 트래픽 전환의 완료를 뜻하지 않는다.

## 2. 적용 범위와 전제조건

- **원본:** AWS EKS. 데모 원본은 Application Deployment·Pod, PostgreSQL 16 StatefulSet·Pod 및 EBS PVC, Service·Ingress·ALB, ConfigMap·Secret과 S3 의존성을 가진다. 원본의 모든 AWS 리소스를 대상에 그대로 복제하는 것은 요구하지 않는다.
- **대상:** AWS EKS, GCP GKE, 온프레미스 K3s. AWS·GCP는 승인된 요구사항에 따라 클라우드 인프라를 구성하고 Kubernetes 리소스를 배포한다. 온프레미스는 사용자가 제공하는 K3s 실행 환경에 Agent를 통해 배포한다. macOS에서 K3s 실행에 필요한 Linux 환경의 도구·사전 준비 범위는 미정이다.
- **P0 우선 리소스:** 대상 Kubernetes 실행 환경, 애플리케이션 Deployment, PostgreSQL 16 StatefulSet, Service, ConfigMap·Secret, DB용 PVC, 외부 접근 네트워크와 대상별 Ingress·Load Balancer, 기본 상태·앱 검증.
- **P0 제외:** AWS/GCP 기존 네트워크 재사용, 객체 스토리지 자동 생성과 객체 데이터 이전. AWS/GCP는 신규 네트워크 구성이며 ON_PREM 기존 K3s 사용은 유지한다. 원본 앱의 S3 기능을 데모에서 어떻게 처리할지는 확인이 필요하다.
- **선행 산출물:** 분석·변환 결과, Sandbox 검증 결과, Sandbox가 실행·검증한 이미지 URI·Digest, 대상 선택과 접근 참조, 계획에 필요한 공통 요구사항. 모든 입력을 사용자가 직접 작성하는 것은 아니다.
- **외부 의존:** 공통 Provider 변환·렌더링·검증 기능, 대상별 인프라·Kubernetes 접근, 이미지 Registry, 계획·승인 기록, 산출물 저장소. 실제 자격 증명은 원문 대신 참조로 다룬다.

## 3. 개념적인 입력 계약

### 3.1. 선행 결과와 수신 시점

Planning은 Build → Sandbox 검증 루프에서 통과한 Build 결과와 실행 검증 Digest를 연결해 입력을 구성한다. 통과 후 다시 빌드하지 않는다. 상위 Workflow가 Analysis·Sandbox·Build 결과와 사용자 대상 선택·별도 `.env` 참조를 전달하고, Planning Activity가 조회·검증·조합한다. Sandbox가 완성된 운영 Spec을 단독으로 생성하는 계약은 요구하지 않는다.

선행 결과에는 검증한 코드·설정·요구사항의 식별자/버전, Sandbox 상태·보고서, Build 소스 출처·최종 이미지 Digest·아키텍처가 필요하다. 코드/설정의 검증 근거와 빌드 출처가 연결되지 않으면 승인 가능한 계획으로 진행하지 않는다. 코드 출처 일치만으로 충분하지 않으며 Build 이미지 Digest와 Sandbox가 실행·검증한 Digest 및 배포 Digest가 같아야 한다. 미검증 플랫폼/설정으로 검증 효력을 확장하지 않는다. 구체적인 수신 정보·참조 형태·미정 사항은 [Provision 입력 계약](input-contract.md)에 기록한다.

### 3.2. Planning이 조합하는 리소스 명세

| 구분 | 받아야 하는 정보 | 예시 | 적용 조건 |
| --- | --- | --- | --- |
| 실행 계획 | Plan ID, Revision, 템플릿 버전 | `plan-001`, `3`, `v1` | 공통 |
| 대상 | Provider, Profile, Region·Zone | `GCP`, `gcp-gke-basic`, `asia-northeast3` | 대상별로 필요한 위치 정보 |
| 접근 권한 | 클라우드 Credential 참조 또는 Agent ID | `credential-001`, `agent-001` | Provider별 |
| 이미지 | URI·Digest, 지원 CPU 아키텍처, Registry 인증 참조 | `repo/app@sha256:…` | 공통 |
| 앱 실행 | CPU·메모리·Replica·Port·Health Check | `500m`, `512Mi`, `1`, `8080`, `/health` | 공통 |
| Kubernetes 컴퓨팅 | Node 수·유형 또는 선정 요구, 디스크 | `1`, `t3.large` | AWS·GCP, 계획에 따라 |
| PostgreSQL | 엔진·버전, DB 이름·사용자, CPU·메모리·용량, 인증 생성 정책 | `postgresql`, `16`, `20GiB` | 공통 |
| 환경변수 | 별도 업로드된 `.env` 참조와 재매핑 요구 | `env-001`, `DB_HOST → database.host` | 공통 |
| VPC·네트워크 | P0 신규 생성의 CIDR·Subnet 요구; 기존 사용 요청은 미지원 | `CREATE`, `10.20.0.0/16` | AWS·GCP |
| Subnet | CIDR, 용도, 리전·AZ | `10.20.1.0/24`, `PUBLIC` | AWS·GCP별 |
| Route·Gateway | 인터넷·아웃바운드·NAT·Endpoint 요구 | `NAT` | AWS·GCP별 |
| 보안 규칙 | 출발지·목적지·프로토콜·포트 | `INTERNET → ALB:443` | 필요한 통신 경로 |
| 외부 접근 | Ingress·LB 유형, Listener·Target·Health Check | `443 → 8080` | Provider별 |
| DNS·TLS | 도메인, 인증서 참조 또는 발급 요구 | `app.example.com` | 외부 HTTPS 공개 시 |
| 온프레미스 | Agent ID, Namespace, StorageClass | `agent-001`, `demo` | ON_PREM |
| 이전 연결 준비 | 허용할 사설 통신 요구 | `DB TCP 5432` | 연결 필요 시 |

예시는 결정된 기본값이 아니다. 필드명·필수/선택 여부·기본값·Provider별 검증 규칙은 후속 설계에서 확정한다. 입력에는 `.env` 원문이나 클라우드 자격 증명을 Workflow 이력에 보관할 목적으로 포함하지 않는다.

## 4. 기능 요구사항 및 개별 수용 기준

### 입력과 대상 변환

#### PROV-001 — 공통 명세 수신 및 검증

Provisioning은 구조화된 공통 `ProvisioningSpec`을 수신하고 실행 전에 입력을 검증해야 한다.

**수용 기준**

- 지원 대상이 아닌 Provider를 지정한 계획은 실행 대상으로 수락하지 않는다.
- 필수 정보의 누락·형식 오류를 실제 리소스 생성 전에 식별하고 항목별 오류를 반환한다.
- 선행 결과의 작업·버전·검증 대상과 Build 출처를 연결하고, Sandbox 미통과·Build 실패·검증 소스 불일치 시 계획 진행을 차단한다. 오류는 민감값 없이 입력 출처와 항목을 식별한다.
- AWS·GCP·ON_PREM에 필요한 입력의 차이를 구분해 검증한다.
- AWS/GCP의 `EXISTING` 네트워크 요청은 P0 미지원 오류로 반환하고 계획·실행으로 진행하지 않는다.

#### PROV-002 — 대상별 리소스 요구사항 해석

공통 앱·DB·네트워크 요구사항을 AWS EKS, GCP GKE, 온프레미스 K3s의 구성 요구로 변환해야 한다.

**수용 기준**

- 동일한 공통 요구사항으로 선택한 대상의 구성을 생성할 수 있다.
- 대상에서 지원하지 않는 요구를 식별하고 이유를 사용자 또는 후속 단계가 확인할 수 있다.
- 명세에 없는 설정을 사전 정의된 정책·템플릿 기본값으로 처리했다면 적용 결과를 Preview에서 확인할 수 있다.

#### PROV-003 — 클라우드 접근 권한 처리

AWS·GCP 자격 증명은 원문이 아닌 참조로 전달받아 필요한 접근을 확인해야 한다.

**수용 기준**

- 자격 증명이 없거나 권한이 부족하면 실패 원인과 필요한 조치 대상을 구분한다.
- 자격 증명 원문을 Preview·일반 로그·실행 결과에 노출하지 않는다.
- 참조(`credential_ref`)는 PROV-022의 환경 연결이 발급한다. AWS는 생성된 assume-role 프로필 이름, GCP 데모는 고정 프로젝트의 ADC다([ADR-005](../../docs/decisions/ADR-005.md)).

#### PROV-004 — 별도 업로드 `.env` 활용

사용자가 소스와 별도로 업로드한 `.env`를 대상 앱의 실행 설정에 반영해야 한다.

**수용 기준**

- `.env` 참조로 필요한 설정을 확인하고 대상에서 사용할 변수와 연결한다.
- 새 PostgreSQL Endpoint·계정 등 실제 생성 결과에 따라 달라지는 값을 주입할 수 있다.
- 변수명, ConfigMap·Secret 참조와 실제 주입 결과의 불일치를 식별한다.
- 비밀값을 일반 로그·Preview에 기록하지 않는다.

### Sandbox와 공통 Provider 모듈

#### PROV-005 — 공통 변환·렌더링 기능 재사용

Sandbox와 Production은 Provider별 설정 변환, Kubernetes Manifest·Helm Values 렌더링, 환경변수·Secret 매핑과 공통 검증 규칙을 재사용할 수 있어야 한다.

Sandbox와 온프레미스 K3s 운영 배포의 공통 입력·렌더 결과는 [공통 배포 계약](onprem-shared-deployment.md)을 따른다. 같은 DB/App Chart를 사용하되 각 Worker의 클러스터 접속·적용·검증·정리 책임은 합치지 않는다.

**수용 기준**

- Sandbox Worker가 Production Provisioning 전체를 실행하지 않고 공통 기능을 사용할 수 있다.
- 공통 렌더링 기능 자체는 실제 클라우드 자원을 만들지 않는다.
- 렌더링 결과를 검토·검증 가능한 산출물로 제공한다.

#### PROV-006 — Sandbox와 Production의 생명주기 분리

Sandbox의 임시 리소스는 Sandbox Worker가, 실제 대상 인프라와 운영 배포는 Provisioning Worker가 관리해야 한다.

**수용 기준**

- Sandbox 검증 중 Production VPC·클러스터·Load Balancer를 생성하지 않는다.
- 임시 검증 자원과 실제 대상 자원의 상태·식별자·정리 책임을 구분한다.
- 공통 코드 재사용만을 위해 Sandbox에 Production 자격 증명을 요구하지 않는다.

### 대상 환경

#### PROV-007 — AWS EKS 환경 구성

승인된 요구사항에 맞춰 AWS에서 앱·PostgreSQL을 배포할 수 있는 EKS 환경을 준비해야 한다. 대상 구성에는 계획에 따른 VPC, Subnet, Route Table, 인터넷·아웃바운드 경로, Security Group, EKS Cluster·Node Group, Kubernetes 접근 권한, ALB 외부 접근 및 PostgreSQL 영속 스토리지가 포함된다. ALB 기반 Ingress에 필요한 AWS Load Balancer Controller 등은 후속 구성 설계에서 검토한다.

**수용 기준**

- 승인된 네트워크·보안 요구사항에 맞는 EKS 대상 구성을 확인할 수 있다.
- 대상 Kubernetes에 접속하여 앱과 DB 리소스를 배포할 수 있다.
- 승인된 외부 접근 요구에 맞는 애플리케이션 경로를 확인할 수 있다.
- 생성하거나 사용한 주요 리소스 ID와 상태를 반환한다.

#### PROV-008 — GCP GKE 환경 구성

승인된 요구사항에 맞춰 GCP에서 GKE 실행 환경을 준비해야 한다. 대상 구성에는 계획에 따른 VPC Network, Subnet, Route·아웃바운드 경로, Firewall Rule, GKE Cluster·Node Pool, Kubernetes 접근 권한, 외부 Load Balancer·Ingress 및 PostgreSQL 영속 스토리지가 포함된다.

**수용 기준**

- GCP 네트워크 모델에 맞는 대상 구성과 보안 규칙을 확인할 수 있다.
- 대상 Kubernetes에 앱과 DB를 배포할 수 있다.
- 승인된 서비스 외부 접근과 상태 확인이 가능하다.
- 생성하거나 사용한 주요 리소스 ID와 상태를 반환한다.
- AWS의 Subnet·AZ 구조를 GCP에 그대로 복제한 결과를 요구하지 않는다.

#### PROV-009 — 온프레미스 K3s 배포

사용자가 제공한 K3s 실행 환경에 Agent를 통해 앱·PostgreSQL을 배포해야 한다.

P0에서는 공개 회원가입 없이 최소 소유자 기록에 Agent를 연결한다. Agent 등록·인증은 [공통 배포 계약](onprem-shared-deployment.md)의 소유 경계를 따르며, 실제 운영 Apply는 승인된 Plan에만 허용한다.

**수용 기준**

- Agent 연결·클러스터 접근·대상 자원 충족 여부를 확인하고 연결 불가 시 실패 또는 대기 상태와 원인을 반환한다.
- 다른 소유자·Agent로 등록하거나 작업을 가져갈 수 없어야 하며 등록 토큰 재사용·만료는 거부한다.
- Namespace를 준비하고 앱·PostgreSQL 배포 상태를 확인한다.
- PVC와 StorageClass 사용 가능성 및 PostgreSQL 영속 상태를 확인한다.
- Data Migration과 Traffic Migration이 사용할 대상 접근 참조를 제공한다.

온프레미스 중계 서버·Envoy·WireGuard의 전체 생명주기를 Provisioning이 소유한다고 가정하지 않는다. 사용자 K3s 환경의 사전 준비 범위도 미정이다.

### Kubernetes 워크로드

#### PROV-010 — Application Deployment

Sandbox를 통과한 정확히 동일한 이미지 Digest를 재빌드 없이 승인된 앱 실행 요구사항에 따라 대상 Kubernetes에 배포해야 한다.

**수용 기준**

- 이미지 URI와 Digest를 배포 정보에서 확인할 수 있다.
- CPU·메모리·Replica·Port·Health Check 요구사항이 반영된다.
- Pod 실행·Ready 상태를 확인하고 이미지 Pull 실패 등 오류를 식별한다.

#### PROV-011 — PostgreSQL 16 배포

대상에 PostgreSQL 16 StatefulSet·Service·PVC를 구성하고 데이터 이전에 필요한 DB 실행 환경을 제공해야 한다.

**수용 기준**

- PostgreSQL 16 StatefulSet과 앱의 DB 접근용 Service를 확인할 수 있다.
- PVC 연결과 영속 스토리지 상태를 확인할 수 있다.
- 승인된 DB 이름·사용자·인증 정책을 반영하며 비밀번호 원문을 일반 출력에 노출하지 않는다.
- 대상에서 필요한 복제 역할·설정을 적용할 수 있는지 검증할 수 있다.

실제 원본 데이터 복사와 CDC 실행은 Data Migration의 책임이다.

#### PROV-012 — 환경변수·Secret 연결

앱이 대상 PostgreSQL 및 필요한 서비스에 연결되도록 일반 설정과 비밀값을 구분해 주입해야 한다.

**수용 기준**

- DB Host·Port 등 대상 리소스 생성 결과에 따른 값이 반영된다.
- `.env`의 일반 설정과 비밀값이 ConfigMap·Secret 등 적절한 참조로 구분된다.
- 누락 변수와 잘못된 ConfigMap·Secret 참조를 식별한다.

#### PROV-013 — Kubernetes 네트워크 구성

앱·PostgreSQL 내부 통신과 승인된 외부 접근 요구사항을 충족해야 한다.

**수용 기준**

- 필요한 Service와 Ingress 또는 대상별 외부 접근 구성을 확인할 수 있다.
- 앱과 DB의 내부 통신이 가능하고 DB가 불필요하게 외부에 공개되지 않는다.
- Provider별 네트워크 정책·Ingress·Load Balancer 차이를 반영한다.
- 승인된 `network_intent`를 대상 Namespace의 NetworkPolicy(기본 차단 + 의도 경로 + 앱 → DB + 외부 진입 포트)로 만들고, 대상이 정책을 실제로 강제해야 한다(EKS VPC CNI, GKE Dataplane V2, K3s 내장). 결정은 티켓 26.
- 실제 대상에서 적용된 연결 경로를 검증할 수 있다.

### Preview와 사용자 승인

#### PROV-014 — Provisioning Preview

실제 대상 자원을 생성하기 전에 사용자에게 계획을 제시해야 한다. Preview에는 대상 Provider·위치, 주요 생성·변경 자원, VPC·Subnet·Routing, 보안 규칙, Kubernetes·앱·DB 사양, 외부 접근, 관련 Manifest·Helm Values·인프라 계획 참조와 검증 오류를 포함한다.

**수용 기준**

- 실제 생성·변경 실행 전에 Preview를 생성한다.
- 검증에 실패한 계획을 승인 가능한 계획으로 제공하지 않는다.
- Preview에 Secret·자격 증명 원문을 노출하지 않는다.
- 대상별 주요 변경 내용과 관련 산출물 참조를 사용자가 확인할 수 있다.

Preview를 위한 대상 조회 API 또는 검증 기능의 사용은 허용하지만, 승인 전 실제 대상 자원을 생성하지 않는다.

#### PROV-015 — 계획별 사용자 승인

실제 Provisioning은 사용자가 특정 계획을 승인한 후에만 실행해야 한다.

**수용 기준**

- 승인 전 실제 대상 인프라 생성이 시작되지 않는다.
- 승인 기록에서 Plan ID와 Revision을 식별할 수 있다.
- 사용자가 본 계획과 실제 실행할 구성이 일치하는지 확인한다.
- 계획 변경 후 이전 Revision의 승인을 재사용하지 않는다.
- 거절·수정 요청을 받은 계획으로 실제 Apply를 진행하지 않는다.

### 실행·실패·검증

#### PROV-016 — 실제 Provisioning 실행

승인된 계획에 따라 Provider별 대상 자원을 구성하고 Kubernetes 워크로드를 배포해야 한다.

**수용 기준**

- 선택한 대상에 맞는 실행 경로가 사용된다.
- 단계별 진행·성공·부분 실패를 조회할 수 있다.
- 생성·재사용한 리소스의 식별자를 기록한다.
- 실제 실행 결과를 승인된 계획과 연결할 수 있다.

#### PROV-017 — 실패·재시도·복구 대응

장시간 실행 중 Worker 장애나 외부 API 실패가 발생해도 실제 대상 상태를 확인하고 안전하게 재개·실패 처리할 수 있어야 한다.

**수용 기준**

- 일시적 오류와 입력·권한 등 자동 재시도에 적합하지 않은 오류를 구분한다.
- 재시도 전 이미 생성된 리소스와 실행 상태를 조회한다.
- 동일 작업의 재시도로 동일 리소스를 무조건 중복 생성하지 않는다.
- 부분 생성 자원과 Worker 재시작 후 확인 가능한 상태를 보고한다.

Terraform State·Helm Release를 이용한 구체적 복구 방식, 자동 정리 여부와 취소 후 자원 삭제 정책은 후속 설계 대상이다.

#### PROV-018 — 실제 배포 후 상태 검증

Provisioning 후 실제 대상 환경에서 클러스터 접근, Worker Node, Deployment·StatefulSet·Pod Ready, Service·Ingress, PostgreSQL 접근, PVC·StorageClass, 앱 헬스, 설정·Secret 매핑과 외부 접근 경로를 검증해야 한다.

**수용 기준**

- 필수 준비 검증 중 하나라도 실패하면 대상을 `READY`로 보고하지 않는다.
- 실패한 리소스·검사항목과 원인을 다음 단계가 확인할 수 있다.
- Sandbox에서 사용한 기능 검증을 실제 배포 후 가능한 범위에서 재사용할 수 있다.
- 데이터 이전 전의 **인프라·서비스 준비**와 데이터 이전 후의 **전체 기능·데이터 검증**을 구분한다.

### 결과와 다음 단계

#### PROV-019 — 구조화된 ProvisioningResult 반환

실행 결과는 후속 단계가 소비할 수 있는 구조화된 `ProvisioningResult`로 제공해야 한다.

| 구분 | 제공할 정보 | 사용 목적 |
| --- | --- | --- |
| 작업 | Plan ID·Revision, 실행 상태, 대상 Provider | 실행 추적 |
| 인프라 | Cluster·기타 리소스 ID, Region 등 | 상태·소유 자원 확인 |
| 앱 | 내부·외부 Endpoint, 서비스 Port | Traffic Migration·검증 |
| PostgreSQL | Host·Port·DB Name, 계정·Secret 참조 | Data Migration |
| Kubernetes | Namespace, 워크로드 식별자 | 상태 조회 |
| 설정 | 적용된 일반 설정·Secret 참조 | 연결 검증 |
| 산출물 | Manifest·Helm Values·Terraform 계획·결과 참조 | 추적·디버깅·인계 |
| 검증 | 검사별 상태와 실패 원인 | 다음 단계 진행 판단 |

**수용 기준**

- Data Migration이 대상 PostgreSQL의 연결 정보와 Secret 참조를 얻을 수 있다.
- Traffic Migration이 대상 앱 Endpoint와 준비 상태를 얻을 수 있다.
- 비밀번호 원문을 반환하지 않는다.
- 인프라 생성 실패와 생성 후 검증 실패를 구분한다.

#### PROV-020 — Data·Traffic Migration 연동

Provisioning은 대상 준비 결과와 연결 참조를 다음 Migration 단계에 넘겨야 한다.

**수용 기준**

- Data Migration에 필요한 대상 DB 접근 정보를 제공한다.
- Traffic Migration에 필요한 대상 서비스 Endpoint·준비 상태를 제공한다.
- 데이터 이전이 아직 완료되지 않았다면 전체 마이그레이션 완료로 보고하지 않는다.
- 최종 DNS 전환이나 Envoy 가중치 조정을 Provisioning이 직접 실행하지 않는다. Route 53·Envoy는 논의 중인 방식이다.

### 보안

#### PROV-021 — 비밀정보 보호

클라우드 자격 증명, 업로드된 `.env`의 비밀값, PostgreSQL 비밀번호, Registry 인증, Kubernetes Secret과 Terraform Plan·State에 포함될 수 있는 민감정보를 보호해야 한다.

**수용 기준**

- 일반 로그와 사용자 Preview에 비밀값 원문을 노출하지 않는다.
- Temporal Workflow 입력·이력·Activity 결과에 불필요한 비밀값 원문을 남기지 않는다.
- 인증 정보는 필요한 시점에 참조로 조회한다.
- 민감 산출물은 접근 권한이 제한된 위치에 보관한다.

### 환경 연결

#### PROV-022 — 원본·대상 환경 연결 발급

2026-11-05 신규 ID다. 분석 스펙의 "원본 접근·대상 환경 연결 발급"(Provisioning 담당)을 요구사항으로 옮겼다. 사용자는 장기 비밀 없이 원본 AWS(읽기)·대상 AWS(구축)·대상 GCP 연결을 등록·확인하고, 플랫폼은 그 연결을 분석·Provisioning이 쓰는 `credential_ref`로 발급한다. 방식은 [ADR-005](../../docs/decisions/ADR-005.md)를 따른다.

**수용 기준**

- AWS 연결은 계정 간 역할 위임으로만 맺는다. 플랫폼이 연결마다 External ID를 발급하고, 사용자는 플랫폼이 제공한 CloudFormation 템플릿으로 역할을 만든 뒤 Role ARN만 등록한다. 액세스 키·서비스 계정 키 JSON은 받지 않는다.
- 원본 역할은 분석 스캔이 실제로 부르는 읽기 동작과 기준점 조회만 허용한다. 대상 역할은 플랫폼이 만든 리소스로 범위를 좁힌 생성 권한을 가진 별도 역할이다.
- 연결 확인은 역할 위임(External ID 포함)과 호출자 확인으로 계정을 확인한다. 원본은 등록 리전의 EKS 클러스터 목록까지 확인한다. 실패는 고정 코드로 남긴다. GCP는 고정 프로젝트와 예상 ADC 주체를 확인한다.
- 계정 전체를 스캔하지 않는다. 플랫폼은 기준점(리전 → EKS 클러스터) 조회만 제공하고, 참조 추적과 범위 확정은 분석 담당이다.
- 연결·Agent·터널 상태를 한 번에 보는 운영자 목록을 제공한다. 운영자 키로만 호출된다.
- 저장하는 값은 Role ARN·External ID·확인 메타데이터뿐이다. 임시 자격 증명은 DB·응답·로그·Temporal 이력에 남지 않는다.

## 5. 책임 제외 범위

| 제외 대상 | 담당 영역 |
| --- | --- |
| 원본 AWS EKS Scanner | Analysis |
| 기준점 이후 참조 추적·분석 범위 확정 | Analysis |
| 소스코드 AI 수정 | Analysis / AI 변환 |
| Sandbox 환경 구축·테스트·정리 | Sandbox |
| 최종 애플리케이션 이미지 빌드 | Build |
| 원본 PostgreSQL 복사·CDC | Data Migration |
| 트래픽 가중치 변경·최종 DNS Cutover | Traffic Migration |
| 객체 스토리지 데이터 이전 | P0 제외 / 향후 Data Migration 범위 검토 |
| 이전 완료 후 사용자 서비스 지속 운영 | 운영권 인계 정책에 따름 |

Provisioning은 다음 단계에 필요한 접근 참조와 Endpoint를 제공하지만, 다음 단계의 실행 책임까지 소유하지 않는다.

## 6. 통합 수용 조건

기능 인수 시에는 `PROV-001`~`PROV-022`의 개별 수용 기준을 적용한다. 특히 다음 결과가 함께 확인되어야 한다.

1. AWS EKS, GCP GKE, 온프레미스 K3s 중 선택한 대상에서 승인된 요구사항에 맞게 계획·배포·상태 검증이 이어진다.
2. 별도 `.env`와 이미지 Digest가 대상 실행 설정에 반영되며 비밀값 원문은 Preview·로그·반환 결과에 나타나지 않는다.
3. Sandbox의 성공과 실제 대상의 준비·검증 결과가 구별되고, 승인 전 실제 대상 생성이 발생하지 않는다.
4. 실패·부분 성공·재시도 후 실제 리소스 상태와 결과를 추적할 수 있다.
5. `ProvisioningResult`가 DB·앱 접근 정보를 후속 Worker에 제공하되 데이터·트래픽 이전 완료를 주장하지 않는다.

P0 공통 결정과 병렬 착수 조건은 [ADR-002](../../docs/decisions/ADR-002.md), [병렬 작업 규칙](parallel-work.md)을 따른다. DB/App Chart 분리, 중앙 S3 State, 승인 DB 기록 유지 및 범용 Outbox 보류는 합의됐으며 구현 완료와 구분한다.

## 7. 확인 필요

- 발표 중 EKS·GKE 클러스터를 실제 생성할지, 일부를 사전 구축할지
- AWS·GCP 신규 네트워크의 Node 유형·규모·영속 스토리지 사양. 기존 VPC 재사용은 P0 제외로 결정됐다.
- 온프레미스 K3s 및 macOS용 Linux 환경의 사전 준비 범위
- 외부 접근 도메인·TLS 인증서의 준비·발급 방식
- 온프레미스 중계 서버의 장기 운영·인계와 WireGuard 연결 경계
- Terraform 부분 실패 후 자동 정리 여부, Provisioning 취소와 실제 리소스 삭제 정책
- 대상 Provider별 이미지 Registry Pull·인증 방식
- 원본 앱의 S3 의존 기능을 P0 테스트에서 처리·시연할 방법
- 실제 운영환경에서 다시 실행할 기능 테스트의 범위와 테스트 데이터·계정·정리 규칙
- `ProvisioningSpec`·`ProvisioningResult`의 구체적인 필드·버전·Provider별 검증 규칙 및 승인된 Plan의 변경 방지 방식
- Route 53·Envoy의 최종 채택 여부. 해당 방식의 가중치·DNS 조작은 Provisioning 책임에서 제외한다.

현재 저장소의 Temporal·API 코드는 AWS→GCP 예제 스캐폴드이며 이 명세의 EKS/GKE/K3s Provisioning을 구현하지 않는다. 이 차이는 기존 제품·아키텍처 문서의 요구사항과 충돌하기보다 **구현 범위가 아직 미달한 상태**로 기록한다.
