# Cloud Migration Platform — 모듈 책임 경계

## 1. 경계 정의 원칙

이 문서의 Worker는 **논리적 실행 책임**이다. Worker마다 별도 서버·저장소·프로세스가 필요하다는 뜻이 아니다. 세부 Task Queue, Workflow 분할과 함수 인터페이스는 후속 설계에서 정한다. 제품 범위는 [제품 정책](../product/policies.md), 전체 연결은 [아키텍처 개요](overview.md)를 따른다.

한 단계의 산출물은 다음 단계가 필요한 만큼만 구조화하여 전달한다. 큰 파일은 산출물 참조로 다룰 수 있지만 `.env` 원문과 Secret을 불필요하게 Workflow 이력·로그에 남기지 않는다. **Sandbox의 검증 책임, 실제 대상 Provisioning 책임, 데이터 복제 책임, 트래픽 제어 책임을 분리**한다.

## 2. Analysis Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 원본 AWS 인프라·EKS·Kubernetes 리소스, 소스코드, 별도 `.env`, 실행 의존관계를 분석한다. AI를 활용해 대상과의 차이 및 필요한 코드·설정 변환과 공통 요구사항을 도출한다. |
| 주요 입력 | AWS 접근 정보 참조, 원본 EKS 정보, 소스코드 참조, `.env` 참조, 대상 환경 |
| 주요 출력 | 인프라 분석 결과, 앱 실행 요구사항, 변경된 코드·설정 참조, 검증 입력, Provisioning 계획에 필요한 요구사항 |
| 책임 제외 | 실제 대상 인프라 생성, 운영 데이터 복제, 트래픽 변경 |
| 의존 모듈 | AWS·Kubernetes 조회, AI 분석·변환 수단(Claude Agent SDK는 후보), 공통 계약·산출물 저장소 |

AI의 출력은 곧바로 인프라 실행 명령이 되지 않는다. Sandbox 검증, 명세 검증, Preview와 사용자 승인을 거친다. 무엇을 자동 수집·변환할 수 있는지는 후속 기능 명세에서 정한다.

## 3. Sandbox Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 미리 준비된 Kubernetes Sandbox 클러스터에 검증 작업별 임시 Namespace·리소스를 만들고 앱·PostgreSQL 테스트 환경을 구성한다. 환경변수·Secret·Service 연결, 실행·헬스·DB 읽기·쓰기와 필요한 기능을 테스트하고 임시 자원을 정리한다. |
| 주요 입력 | 변환 코드·설정 참조, 테스트용 이미지 또는 빌드 입력, 공통 리소스 요구사항, 테스트 데이터·검증 조건 |
| 주요 출력 | 항목별 성공·실패, 오류·로그·테스트 결과 참조, 정리 결과 |
| 책임 제외 | Production VPC·클러스터·Load Balancer 생성, 실제 운영 DB 임의 변경, 대상 환경의 최종 성공 판정 |
| 의존 모듈 | 공통 Provider 변환·렌더링·검증, Helm 차트, Kubernetes API, 기능 테스트, 산출물 저장소 |

Sandbox는 변환 오류를 사전에 찾는 곳이다. **Sandbox 성공은 실제 EKS·GKE·K3s에서의 정상 동작을 보장하지 않는다.** Sandbox 환경의 구축·검증·정리는 이 Worker의 책임이며, Provisioning Worker의 실행 전체를 재사용하지 않는다.

## 4. Build Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 검증된 코드로 최종 컨테이너 이미지를 빌드하고 Registry에 Push한다. 대상 CPU 아키텍처와 호환성에 필요한 정보를 제공한다. |
| 주요 입력 | 검증을 통과한 소스·설정 참조, 빌드 정의와 대상 아키텍처 요구사항 |
| 주요 출력 | 이미지 URI·Digest, 빌드 결과·호환성 정보 |
| 책임 제외 | 실제 대상 인프라 생성, 데이터 이전, 트래픽 전환 |
| 의존 모듈 | Sandbox 검증 결과, 이미지 빌드 수단, AWS ECR, 산출물 저장소 |

최종 배포 이미지는 **검증된 코드**에서 생성해야 한다. Sandbox 실행을 위해 사전 이미지가 필요한지, 검증 후 최종 이미지를 어떻게 동일 코드·설정과 연결할지는 후속 설계에서 정한다. 데모 기본 이미지 Registry는 ECR이지만 배포 대상의 이미지 접근 방식은 별도 설계가 필요하다.

## 5. Provisioning Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 공통 Provisioning 명세를 검증하고 대상 Provider별 인프라·Kubernetes 구성을 만든다. 실행 전 Preview를 제공하며, 사용자 승인 후 실제 인프라와 앱·DB 리소스를 배포한다. `.env` 매핑과 대상 연결 정보·Secret 주입, 실제 리소스 상태 검증까지 맡는다. |
| 주요 입력 | 검증된 요구사항·설정, 이미지 URI·Digest, 대상 환경, 공통 Provisioning 명세, 승인된 계획 참조 |
| 주요 출력 | Preview·실행 산출물, 생성 리소스 ID, 배포 상태, 앱·PostgreSQL Endpoint, 연결·Secret 참조, 실제 환경 검증 결과 |
| 책임 제외 | Sandbox 클러스터 운영·임시 Namespace 정리, PostgreSQL 데이터 복제 자체, 트래픽 가중치 제어 |
| 의존 모듈 | 공통 Provider 모듈, Terraform, Helm, Kubernetes API, AWS/GCP 연동, 온프레미스 Agent, 이미지 Registry |

대상은 AWS EKS, GCP GKE, 온프레미스 K3s다. Preview와 승인된 계획의 일치 방식을 후속 설계에서 정해야 하며, **승인 전 실제 인프라를 생성하지 않는다.** Provisioning은 Data Migration에 DB 접근 참조를, Traffic Migration에 신규 서비스 접근 정보와 준비 상태를 제공한다. 실제 배포 후의 상태·헬스·기능 검증은 Sandbox 검증과 별개의 결과다.

## 6. Data Migration Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 원본·대상 PostgreSQL 연결을 준비하고 초기 데이터 복사와 Logical Replication 기반 변경 동기화를 진행한다. 복제 상태·지연·정합성을 확인하고 DB Cutover에 필요한 작업을 수행한다. |
| 주요 입력 | 원본·대상 DB 접근 참조, 대상 DB 준비 결과, 필요한 사설 연결 정보, 데이터 이전 정책 |
| 주요 출력 | 초기 복사·복제 상태, 복제 지연, 정합성 결과, DB 전환 가능 여부와 전환 결과 |
| 책임 제외 | 대상 DB 인프라 생성, 앱 이미지 빌드, HTTP 트래픽 가중치 제어, P0 객체 스토리지 자동 이전 |
| 의존 모듈 | Provisioning 결과, PostgreSQL 복제·검증 수단, 필요 시 WireGuard 연결, 공통 계약·산출물 저장소 |

PostgreSQL 논리 복제가 모든 DB 객체나 변경을 자동 처리한다고 가정하지 않는다. 실제 DB Cutover, 쓰기 제어, 역방향 복제 및 Cutover 후 복구 절차는 **확인 필요**다. Traffic Migration은 데이터 동기화와 전환 가능 여부를 전달받아야 한다.

## 7. Traffic Migration Worker

| 항목 | 내용 |
| --- | --- |
| 책임 | 원본 AWS와 대상 서비스의 접근 경로·준비 상태를 확인하고, 단계적 트래픽 이전, 단계별 관찰과 가능한 범위의 복귀, 최종 네트워크 구성 정리·인계 연계를 담당한다. |
| 주요 입력 | 원본 AWS ALB·신규 앱 Endpoint, 대상 준비 결과, 데이터 이전·정합성 상태, 전환·롤백 정책 |
| 주요 출력 | 단계별 트래픽 상태, 헬스·라우팅 결과, 복귀·최종 전환 결과, 인계에 필요한 네트워크 정보 |
| 책임 제외 | PostgreSQL 초기 복사·복제, 대상 인프라 생성, 사용자 승인 정책 결정 |
| 의존 모듈 | Provisioning·Data Migration 결과, 상태 관측, 추후 확정될 트래픽 제어 수단 |

현재 **논의 중인 안**은 `사용자 → Route 53 → Envoy 중계 → 원본 AWS ALB/EKS 또는 대상 GKE·K3s`다. 이 안에서는 Route 53이 진입점을, Envoy가 가중치 전환을 담당한다. 온프레미스 NAT 환경에는 WireGuard 연결을 고려한다. 최종 채택, 중계 서버의 구축·운영·제거·인계, 데이터 Cutover 전후의 롤백 경계는 확정되지 않았다. 트래픽만 원본으로 되돌리는 방식이 항상 데이터 복구를 의미하지는 않는다.

## 8. 공통 Provider 모듈

| 항목 | 내용 |
| --- | --- |
| 책임 | 공통 Provisioning 요구사항 해석, Provider별 설정 변환, Kubernetes Manifest·Helm Values 렌더링, 환경변수·Secret 매핑 규칙과 공통 리소스 검사 제공 |
| 주요 입력 | 검증된 공통 요구사항, 대상 Provider, 이미지·리소스·연결 요구사항 |
| 주요 출력 | 변환된 대상별 설정, 렌더링된 배포 산출물, 공통 검증 결과 |
| 책임 제외 | 특정 Worker의 실행 상태 관리, Terraform Apply, 클라우드 자원 생성, Sandbox 테스트·정리 |
| 의존 모듈 | 경계별 계약, 검증된 템플릿·Helm 차트, 대상별 설정 규칙 |

Sandbox와 Production Provisioning은 같은 Python 모듈을 직접 재사용할 수 있다. Deployment·StatefulSet 요구사항, ConfigMap·Secret 매핑, 공통 Helm 렌더링, Service·Pod 검사와 리소스 검증은 공통화 후보로 둔다. **EKS·GKE·K3s 클러스터 구성, IAM, 네트워크, Load Balancer, CSI·StorageClass와 Provider 인증**은 대상별 구현이 필요하다. 공통 모듈의 최종 패키지 구조·함수 시그니처는 미정이다.

기능 테스트 코드도 가능한 경우 Sandbox와 실제 배포 후 검증에 재사용한다. 테스트 대상 주소·인증·연결 정보는 실행 환경에 맞게 주입한다. 운영 환경에서 테스트가 데이터에 영향을 주는지, 테스트 계정과 정리 규칙이 무엇인지는 별도로 정해야 한다.

## 9. FastAPI·Temporal·PostgreSQL·저장소의 역할

| 구성요소 | 보유하는 책임 | 경계 밖 |
| --- | --- | --- |
| FastAPI | 외부 요청 접수, 작업·계획 조회, Preview·승인·취소 인터페이스, SSE 상태 전달 | HTTP 요청 안에서 전체 이전을 동기적으로 실행 |
| 업무 PostgreSQL | 사용자·작업 메타데이터, 계획·승인·인계 등 비즈니스 기록 | Workflow 실행 이력·Task Queue |
| Temporal Server | 장시간 작업의 순서·대기·재시도·실행 이력·Task Queue | 사용자 비즈니스 기록, 소스·이미지 파일 저장 |
| AWS S3 | 업로드 소스, 변환 결과, 테스트·계획 등 파일 산출물 | Workflow 상태, P0 사용자 객체 스토리지 자동 이전 |
| AWS ECR | 컨테이너 이미지·Digest | 계획·승인 기록 |

FastAPI는 Temporal에 장시간 작업을 맡기고 상태를 조회한다. Temporal Workflow는 순서와 승인 대기를 조정하지만 AWS API·AI 요청·Terraform·Helm·DB·트래픽 변경 같은 부수효과를 직접 수행하지 않는다. 담당 Activity가 외부 작업을 실행하며, 재시도에 대비해 중복 적용을 막는 기준이 필요하다. Temporal이 외부 작업의 정확히 한 번 실행을 자동 보장한다고 가정하지 않는다.

## 10. Worker 사이 입력·출력 및 의존 관계

Provisioning Planning이 소비할 정보는 [Provision 입력 계약](../../specs/provisioning/input-contract.md)에 구체화한다. 상위 Workflow가 Analysis·Sandbox·Build 및 사용자 선택의 결과 참조를 연결하고 Planning Activity가 조회·검증·명세 조합을 수행하는 설계다. Sandbox는 검증 대상 코드·설정·요구사항 버전과 보고서를, Build는 사용한 소스·설정과 최종 이미지 Digest의 연결을 제공한다. 상세 wire schema는 각 담당 영역과 합의해야 하며, 이 문서 변경이 상위 Worker 구현을 변경하지 않는다.

| 경계 | 전달 정보 | 받는 쪽의 책임 |
| --- | --- | --- |
| API → Analysis | 원본 AWS 접근 참조, 소스·`.env` 참조, 대상 환경 | 원본 분석·요구사항 도출 |
| Analysis → Sandbox | 변환 코드·설정과 검증 대상 | 임시 환경에서 실행·검증·정리 |
| Sandbox → Build / Planning | 테스트 결과·오류 및 검증된 코드 참조 | 최종 이미지와 계획 준비 |
| Build → Provisioning | 이미지 URI·Digest·호환성 정보 | 계획·배포 입력으로 사용 |
| Analysis / Planning → Provider 모듈 | 공통 Provisioning 요구사항 | 대상별 설정 렌더링 |
| Provider 모듈 → Sandbox / Provisioning | 공통 또는 대상별 Manifest·Helm Values | 검증 환경 또는 실제 환경에 적용 |
| Provisioning Planning → API / 사용자 | Preview·변경 정보 | 실제 실행 전 검토·승인 |
| Provisioning → Data Migration | 대상 DB Endpoint·접근 참조·준비 상태 | 데이터 복사·복제·정합성 검사 |
| Provisioning → Traffic Migration | 원본·대상 앱 Endpoint·준비 상태 | 접근 경로와 라우팅 변경 |
| Data Migration → Traffic Migration / 검증 | 동기화·정합성·전환 가능 여부 | 안전한 전환 판단에 반영 |
| Traffic Migration → 최종 검증 / 인계 | 단계별 라우팅·헬스·최종 상태 | 실제 서비스 검증과 운영 산출물 인계 |

계약 필드와 전달 방식은 이 단계에서 확정하지 않는다. `.env` 원문·비밀번호를 불필요하게 Workflow 입력·이력·로그에 넣지 않고, Terraform Plan·State는 민감 정보 가능성을 고려해 접근 통제와 안전한 저장 방식을 후속 설계한다.

## 11. 공통 코드 재사용과 책임 충돌 방지

- **Analysis ↔ Sandbox:** Analysis는 검증할 코드·설정을 제공한다. Sandbox가 임시 실행·테스트·정리와 실패 원인 반환을 책임진다.
- **Sandbox ↔ Provisioning:** 공통 Provider 라이브러리와 기능 테스트는 공유할 수 있으나, Sandbox는 Production 인프라를 만들지 않고 Provisioning은 Sandbox 임시 자원을 정리하지 않는다.
- **Build ↔ Sandbox:** 최종 이미지는 검증된 코드에서 나온다. Sandbox용 이미지의 생성 시점과 최종 이미지의 동일성 확인 방식은 별도 설계한다.
- **Provisioning ↔ Data Migration:** Provisioning은 대상 DB와 연결 참조를 준비한다. Data Migration이 원본 데이터를 복사·동기화하고 정합성을 검사한다.
- **Provisioning ↔ Traffic Migration:** Provisioning은 앱 Endpoint와 준비 상태를 제공한다. Traffic Migration이 라우팅 가중치·전환·복귀를 실행한다.
- **FastAPI ↔ Temporal:** API는 외부 요청과 승인 인터페이스를 제공한다. Temporal은 장시간 작업의 순서·상태·대기를 관리한다.
- **Provider 모듈 ↔ Worker:** 공통 모듈은 변환·렌더링·검증 로직을 제공한다. Worker가 실행 맥락, 외부 부수효과와 정리 책임을 가진다.

## 12. 확인 필요

- 기존 문서·Temporal 예제 코드의 플랫폼 관리 앱·독립 K3s·Cloudflare 방향을 새로운 AWS EKS 원본·EKS/GKE/K3s 대상 정책에 맞춰 정렬할 시점과 범위
- Analysis의 AI 도구 최종 선택, 자동 코드 수정 범위와 검증 실패 처리
- Sandbox 실행에 필요한 이미지 생성 시점, 최종 이미지 Digest와 검증 결과 연결 방식
- 공통 Provisioning 명세와 Provider 모듈의 최종 범위·계약·패키지 구조
- 대상 클러스터 사전 준비 범위와 AWS/GCP/온프레미스 인프라·권한·네트워크 세부 사양
- PostgreSQL Logical Replication의 세부 구성, DB Cutover·역방향 복제·정합성 기준
- Route 53·Envoy 안의 채택 여부, 중계 서버·WireGuard의 운영·제거·인계 주체와 전환 후 복구 경계
- Workflow 분할·Task Queue·Worker 배포 단위, Activity 재시도·Heartbeat·멱등성 정책
- 실제 배포 환경에서 기능 테스트 재실행 시 테스트 데이터·계정·정리 정책
- P0에서 객체 스토리지 기능을 보여줄 수준. 자동 생성·데이터 이전은 P0 필수 범위에서 제외한다.
