# Cloud Migration Platform — 전체 아키텍처

## 1. 목적과 문서 범위

이 문서는 AWS EKS에서 실행 중인 Kubernetes 기반 백엔드 애플리케이션을 AWS EKS, GCP GKE 또는 온프레미스 K3s로 이전하기 위한 **논리적 시스템 구조**를 정의한다. 사용자 입력부터 분석·변환·검증·승인·대상 환경 구성·데이터 및 트래픽 이전·운영권 인계까지의 책임 관계를 다룬다. 제품 범위와 제약의 기준은 [제품 개요](../product/overview.md)와 [제품 정책](../product/policies.md)이다.

그림의 컴포넌트는 책임을 나타낸다. 각각 독립 서버나 프로세스로 배포된다는 뜻은 아니며, Worker 수·Task Queue 이름·세부 실행 순서·API 계약은 이 문서에서 확정하지 않는다. 현재 저장소의 Temporal 예제 코드 역시 목표 아키텍처의 구현 완료를 뜻하지 않는다.

## 2. 아키텍처 목표와 원칙

1. 원본 인프라와 애플리케이션 의존성을 분석한 결과를 대상별 구성으로 연결한다.
2. AI가 생성한 코드·설정을 Sandbox에서 먼저 검증하고, 실제 인프라 생성 전 Preview와 사용자 승인을 둔다.
3. Sandbox의 임시 리소스 검증과 Production Provisioning을 별도 책임으로 유지한다. 공통 변환·렌더링·검증 로직은 재사용한다.
4. 장시간 작업의 순서·대기·실패 복구는 Temporal이 조정하고, 외부 시스템에 영향을 주는 작업은 담당 Worker의 Activity가 수행한다.
5. 업무 데이터, Workflow 실행 상태, 파일 산출물, 컨테이너 이미지를 서로 다른 저장 책임으로 다룬다.
6. 실제 배포 후에도 대상 환경의 상태와 기능을 검증한다. Sandbox 통과만으로 실제 이전 성공을 선언하지 않는다.

## 3. 기술 스택과 책임

| 영역 | 기술 | 역할·상태 |
| --- | --- | --- |
| Frontend | Next.js + React | 사용자 입력, 상태 확인, Preview와 승인. 이 백엔드 저장소 밖의 구성 |
| API Server | FastAPI | 작업 생성·조회·승인·취소 요청과 SSE 상태 전달 |
| Workflow Engine | Temporal | 단계 조정, 실행 이력, 재시도, 사용자 승인 대기 |
| Workflow / Activity Worker | Python + Temporal Python SDK | Workflow 실행과 외부 작업 수행 |
| 업무 데이터베이스 | PostgreSQL | 사용자, 작업 메타데이터, 계획·승인 기록 |
| Workflow 영속 상태 | Temporal Server | Workflow 이력·상태와 Task Queue 관리. 업무 DB와 논리적으로 분리 |
| 산출물 저장소 | AWS S3 | 업로드 소스, 변환 결과, 검증 산출물, 실행 계획 보관 |
| 이미지 레지스트리 | AWS ECR | 빌드된 컨테이너 이미지와 Digest 보관 |
| AI 분석·코드 수정 | Claude Agent SDK **후보** | 소스 분석과 코드·설정 변환. 최종 채택 미정 |
| 데이터 계약 검증 | Pydantic | 경계별 구조화 입력·출력 및 Provisioning 명세 검증 |
| 공통 Provider 모듈 | Python | 공통 요구사항 해석, 대상별 설정 변환·렌더링·검증 로직 제공 |
| 클라우드 인프라 구성 | Terraform | AWS·GCP 대상 인프라 구성 |
| Kubernetes 리소스 배포 | Helm | EKS·GKE·K3s의 애플리케이션·DB 리소스 배포 |
| 온프레미스 실행 | Agent + K3s | 사용자 환경의 Kubernetes 배포 연계 |
| 환경 간 연결 | WireGuard | 필요한 사설 마이그레이션 연결의 설계 방향. 적용 범위 미정 |
| DB 데이터 이전 | PostgreSQL Logical Replication | 초기 복사와 이후 변경 동기화 방향. 세부 절차 미정 |
| 트래픽 전환 | Route 53 + Envoy **논의 중** | 점진적 전환·롤백을 위한 설계안. 최종 채택 미정 |
| 진행 상태 전달 | SSE | Frontend에 작업 상태 전달 |

Redis, Celery, Kafka 같은 별도 작업 큐는 현재 확정 스택에 포함하지 않는다. PostgreSQL 업무 DB는 사용자·계획·승인 같은 비즈니스 기록을 보관하고, Temporal은 실행 순서·상태·이력과 Task Queue를 관리한다. S3의 파일 산출물은 Workflow 상태가 아니며, ECR의 이미지는 파일 산출물이나 업무 기록과도 구분한다.

## 4. 시스템 구성도

```text
                    [Next.js / React]
                            │ REST / SSE
                            ▼
                        [FastAPI]
                         │     │
             업무 기록    │     │ 작업 시작·조회·승인
                         ▼     ▼
                 [PostgreSQL] [Temporal Server]
                  업무 DB       Workflow / Task Queue
                                   │ 단계별 작업 전달
               ┌───────────────────┼───────────────────┐
               ▼                   ▼                   ▼
        [Analysis Worker]   [Sandbox Worker]     [Build Worker]
        AWS·EKS·소스·AI     임시 K8s·테스트·정리   이미지 빌드·ECR
               │                   │                   │
               └───────────────────┴───────────────────┘
                                   │
                                   ▼
                         [Provisioning Worker]
                         Preview → 승인 후 실행
                          │                │
                       Terraform         Helm / Agent
                       AWS·GCP           EKS·GKE·K3s
                          └────────┬───────┘
                                   ▼
                         [Data Migration Worker]
                         PostgreSQL 복사·복제·검증
                                   │
                                   ▼
                        [Traffic Migration Worker]
                        점진적 전환·상태·복귀
                                   │
                                   ▼
                          [최종 검증·운영권 인계]

   [공통 Provider 모듈] ── 변환·렌더링·검증 ──> Sandbox / Provisioning
   [AWS S3] ── 소스·변환·검증·계획 산출물 참조
   [AWS ECR] ── 이미지 URI·Digest 제공
```

선은 주된 의존 관계를 간략히 표현한다. Worker가 모두 동시에 실행되어야 한다는 뜻은 아니다. 실제 선후관계와 승인 대기는 Temporal Workflow가 조정한다. 외부 통합에는 원본 AWS·EKS API, 대상 AWS/GCP/Kubernetes API, 온프레미스 Agent와 향후 확정될 트래픽 제어 수단이 포함된다.

## 5. 상위 실행 흐름

| 순서 | 단계 | 책임과 다음 단계로 넘길 결과 |
| --- | --- | --- |
| 1 | 사용자 입력 | API가 AWS 접근 정보 참조, 소스, 별도 `.env`, 대상 환경을 접수 |
| 2 | Infrastructure / Application Analysis | 원본 AWS·EKS와 Kubernetes 리소스, 소스·의존성·환경변수 분석 결과 |
| 3 | AI Planning / Code Adaptation | 대상 차이와 이전 요구사항, 변경된 코드·설정 |
| 4 | Sandbox Verification | 사전 준비된 Kubernetes Sandbox에서 임시 리소스 테스트 후 결과·오류 반환 및 정리 |
| 5 | Image Build | 검증된 코드로 최종 이미지를 빌드·저장하고 URI·Digest 확보 |
| 6 | Provisioning Planning | 공통 Provisioning 명세 검증, 대상별 구성 변환, Preview 생성 |
| 7 | User Approval | 실제 생성·변경 대상에 대한 사용자 승인 또는 수정 요청 |
| 8 | Production Provisioning | 승인 후 Terraform·Helm·Agent 등으로 대상 구성 및 앱·DB 배포, 상태·Endpoint 반환 |
| 9 | Data Migration | 원본·대상 연결, PostgreSQL 초기 복사·변경 동기화·정합성 확인 |
| 10 | Traffic Cutover | 서비스 준비와 동기화 상태를 확인하며 단계적 전환, 가능한 범위의 복귀 관리 |
| 11 | Final Verification & Handover | 실제 서비스 상태 재확인, 결과와 운영 산출물 인계 |

이는 책임의 상위 순서다. Sandbox 테스트에 필요한 이미지와 최종 배포 이미지의 정확한 생성·검증 순서, 단계 병렬화, Activity 분할, 승인 신호와 재시도 방식은 후속 설계에서 정한다. **최종 배포 이미지는 검증된 코드로부터 생성되어야 한다.**

## 6. 주요 컴포넌트와 Temporal 경계

FastAPI는 사용자와의 외부 접점으로 요청을 접수하고, 업무 DB에 작업·계획·승인 기록을 관리하며, Temporal에 장시간 작업을 요청하고 상태를 전달한다. HTTP 요청 안에서 전체 이전을 동기적으로 실행하지 않는다.

Temporal Workflow는 분석, Sandbox, 빌드, 계획, 승인 대기, 실제 Provisioning, 데이터·트래픽 이전의 **순서와 상태**를 조정한다. AWS API 호출, AI 요청, Terraform·Helm 실행, DB 복제와 트래픽 변경 같은 부수효과는 Activity가 담당한다. 재시도 시 중복 리소스가 생기지 않도록 Activity별 멱등성 또는 기존 상태 확인이 필요하다. Temporal이 외부 작업의 정확히 한 번 실행을 자동 보장하는 것은 아니다. Worker의 논리적 분리는 물리 서버 분리를 요구하지 않는다.

Analysis, Sandbox, Build, Provisioning, Data Migration, Traffic Migration의 구체적 입력·출력과 제외 책임은 [도메인 경계](domain-boundaries.md)에 정의한다. Workflow 분할·Child Workflow, Task Queue 개수와 이름, 세부 Retry Policy·Heartbeat·Signal은 이 문서에서 확정하지 않는다.

## 7. 공통 Provider 모듈과 검증 재사용

```text
                      [공통 Provider 모듈]
                   요구사항 해석·설정 변환
                  렌더링·매핑·공통 검증
                         │        │
                         ▼        ▼
                    [Sandbox]  [Provisioning]
                   임시 K8s      실제 대상 인프라
                   기능 검증      구성·배포·상태 확인
                   임시 정리      연결 정보 반환
```

Sandbox와 Provisioning은 같은 Python 변환·렌더링 모듈, Helm 차트, 환경변수·Secret 매핑 규칙과 공통 Kubernetes 검사 로직을 재사용할 수 있다. 공통 모듈은 특정 Worker의 실행 상태에 의존하지 않고, 실제 Terraform Apply나 클라우드 리소스 생성을 수행하지 않는다. Sandbox가 Production Provisioning 전체를 호출할 필요도 없다. 클러스터·IAM·네트워크·Load Balancer·CSI/StorageClass처럼 대상 환경에 따라 달라지는 구성은 Provider별 구현이 필요하다.

Sandbox에서 수행한 기능 테스트는 실제 배포 후에도 가능한 경우 재실행한다. 대상 주소와 인증·연결 정보는 환경별로 주입하고, 실제 환경에서의 테스트 데이터 변경·계정·정리 정책을 고려해야 한다. **Sandbox의 테스트 통과와 실제 대상 환경의 검증 통과는 별도 결과**로 기록한다.

## 8. 주요 데이터와 산출물 흐름

| 데이터·산출물 | 생성·관리 주체 | 소비 주체 |
| --- | --- | --- |
| 사용자 입력 및 `.env` 참조 | 사용자 / API | Analysis |
| 원본 인프라·앱 분석 결과 | Analysis | AI Planning / Provisioning Planning |
| 변환된 코드·설정 | Analysis / AI | Sandbox / Build |
| Sandbox 테스트 결과·오류 | Sandbox | Workflow / Build / Planning |
| 이미지 URI·Digest | Build / ECR | Provisioning |
| 공통 Provisioning 명세 | Planning | Provider 모듈 / Provisioning |
| Manifest·Helm Values | Provider 모듈 | Sandbox / Provisioning |
| Provisioning Preview | Provisioning Planning | Frontend / 사용자 승인 |
| 실제 리소스 ID·Endpoint·연결 참조 | Provisioning | Data / Traffic Migration |
| 복제 상태·정합성 결과 | Data Migration | Traffic Migration / 최종 검증 |
| 트래픽 전환 결과 | Traffic Migration | 최종 검증 / 운영권 인계 |

업로드 소스, 변환 결과, 테스트 결과와 실행 계획 등의 큰 파일은 S3 산출물 참조로 전달할 수 있다. `.env` 원문, Secret과 비밀번호를 불필요하게 Workflow 입력·로그·산출물에 저장하지 않는다. Terraform Plan과 State에는 민감 정보가 포함될 수 있으므로 접근 통제와 안전한 보관 방식이 필요하다. **객체 스토리지 자동 생성·데이터 이전은 P0 필수 범위가 아니다.** 산출물 저장용 S3 사용과 사용자 객체 데이터 이전은 서로 다른 책임이다.

## 9. 확인 필요

- Route 53·Envoy 중계 방식의 최종 채택, 구축·운영 주체, GCP 이전 후 제거 절차와 온프레미스 인계 정책
- WireGuard를 실제로 사용할 연결 구간과 온프레미스 NAT 환경의 접근 방식
- 실제 EKS·GKE 클러스터를 데모 중 생성할 범위와 사전 준비할 자원
- 이미지의 Sandbox 검증용 빌드와 최종 ECR 이미지 빌드·Digest 연결 방식
- PostgreSQL 초기 복사·논리 복제의 적용 범위, DB Cutover, 역방향 복제, 정합성 기준과 단계별 롤백 경계
- 공통 Provisioning 명세의 필드·버전, Provider 모듈의 최종 패키지와 인터페이스
- Workflow·Task Queue·Worker 배포 단위와 세부 재시도·Heartbeat·승인 이벤트 정책
- 업무 DB와 Temporal 저장소의 실제 물리 배치, S3 산출물·Terraform Plan/State의 보관·권한 정책
- 기존 `temporal-flow.md`, 코드, README에 남아 있는 플랫폼 관리 앱·K3s·Cloudflare 및 예제 Worker 구성을 새 제품 정책과 어떻게 정렬할지. 이 문서 범위 밖이므로 이번 단계에서는 수정하지 않는다.
