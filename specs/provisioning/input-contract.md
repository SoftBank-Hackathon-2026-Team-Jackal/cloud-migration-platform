# Provision 수행을 위해 전달받아야 하는 정보

## 1. 상태와 적용 범위

- 작업: [Issue #5](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/5), [티켓 01](../../tickets/provisioning/01-provider-spec-validation.md).
- 2026-11-03 사용자 요청에 따라 기존 정책과 논의한 입력 흐름을 구체화한 **계약 설계안**이다. 실제 Worker/API 구현이나 다른 팀의 wire schema 합의 완료를 뜻하지 않는다.
- 제품 정책과 [spec.md](spec.md)가 우선한다. 이 문서는 입력 정보의 출처·시점·검증을 다루고 [plan.md](plan.md)는 실행 설계를 다룬다.
- 논리적 순서: Analysis/코드 수정 → Build → 해당 Digest로 Sandbox 검증(실패·재수정 시 새 Build) → 통과 Digest로 Planning·Preview → 특정 계획 승인 → Production Provisioning.
- 통과 후 재빌드하지 않는다. Sandbox 실행·검증 Digest와 Production 배포 Digest를 일치시킨다. [ADR-003](../../docs/decisions/ADR-003.md) 기준이며 기존 내부 v1 구현과의 차이는 §10과 B-01에 기록한다.

## 2. 세 가지 입력 경계

| 경계 | 전달 내용 | 받는 쪽 책임 |
| --- | --- | --- |
| 상위 Workflow → Planning | Analysis·Sandbox·Build 결과 참조, 대상 선택·환경설정 참조, 작업 식별자 | Activity에서 참조 조회·권한·무결성 확인 후 선행 결과의 연결 검증 |
| Planning → 공통 Provider 모듈 | 검증된 `ProvisioningSpec` | 순수 Provider 변환·렌더링·검증. 저장소 조회·클라우드 변경 없음 |
| 승인 대기 이후 → 실행 Activity | Plan ID·Revision, 잠긴 산출물과 승인 기록 참조 | 승인·대상·Digest 재확인 후 Terraform/Helm/Agent 실행 |

Sandbox가 전체 Spec을 작성한다고 가정하지 않는다. Sandbox/변환 루프가 공통 Build 모듈을 Activity 경계에서 호출하는 구체 인터페이스는 B-01에서 맞춘다. Workflow가 각 단계 결과를 보유·연결하고 Planning이 조합한다. Plan 식별자 발급·저장은 Planning/업무 기록 경계에서 맡으며 Sandbox의 필수 출력이 아니다. 초기 Planning 요청에는 아직 없는 Preview·승인 기록을 요구하지 않는다.

## 3. Planning이 선행 단계에서 받는 정보

| 출처 | 받아야 하는 정보 | 조건·사용 목적 |
| --- | --- | --- |
| 상위 실행/업무 기록 | 작업 ID, 입력 계약 버전, 결과별 식별자·버전 | 다른 작업·실행 시도의 결과가 섞이지 않도록 연결 |
| Analysis | 분석 결과 참조, 앱·PostgreSQL·네트워크 요구사항 참조/버전, 변환된 코드·설정 참조와 Digest | 명세 구성의 근거. 원본 소스와 변환된 소스를 구분 |
| Sandbox | 검증 상태, 실행한 Build 참조·이미지 Digest·검증 플랫폼, 코드·설정·요구사항 식별자/버전, 결과·테스트 보고서 참조 | 단순 성공 플래그뿐 아니라 무엇을 검증했는지 식별 |
| Sandbox | 사용한 검증 Profile·대상 가정, 환경 매핑 규칙 버전, 정리 결과·잔여 자원 참조 | 대상/설정 변경 시 검증 유효 범위 판단. 정리 상태는 테스트 상태와 분리 |
| Build | 빌드 성공 상태, 사용한 코드·빌드 설정 식별자/Digest, 이미지 URI·Digest, 지원 플랫폼 목록 | Sandbox가 검증한 Build/이미지와 같은지 추적. Build는 Sandbox 통과 전에 실행되며 Registry 인증은 별도 참조 |
| API/업무 DB | 대상 Provider·Profile·위치, 사용자 선택 버전, Credential 또는 Agent 참조 | 대상 선택과 접근 요구. 모두 사용자가 직접 입력할 필요는 없음 |
| 업로드/설정 기록 | 별도 업로드 `.env`의 불변 버전 참조, 키 분류·재매핑 규칙 참조/버전 | 일반 설정·사용자 Secret·동적 바인딩 구분. 원문은 이력에 전달하지 않음 |
| 대상/운영 정책 | 승인 가능한 Node·네트워크·스토리지·외부 접근 요구, 적용 Profile/템플릿 버전 | 필수 요구와 사용자 선택의 충돌 검사; 정책 기본값의 출처 추적 |

Sandbox의 임시 DB 주소·비밀번호·Namespace·PVC를 운영 값으로 복사하지 않는다. 테스트에 사용한 설정은 운영에서 재사용할 매핑 규칙과 환경별 임시 값을 구분한다. 운영 DB 주소·생성 Secret 참조는 실제 생성 후 승인된 바인딩 위치에 채운다.

## 4. 조합된 ProvisioningSpec의 정보

필드명과 JSON 배치는 구현 전 합의할 수 있다. 아래 정보 범주와 조건이 구현의 기준이며 예시 값은 기본값을 확정하지 않는다.

| 구분 | 받아야 하거나 Planning이 구성할 정보 | 조건 |
| --- | --- | --- |
| 계획·출처 | Plan ID·Revision, 계약·템플릿 버전, 선행 결과·대상 선택 버전 연결 | 공통; Planning에서 계획에 고정 |
| 대상 환경 | AWS EKS / GCP GKE / ON_PREM K3s, Profile, 대상별 위치 | Region·Zone은 대상별 필요 조건; ON_PREM에 클라우드 Region 강제 금지 |
| 접근 권한 | Cloud Credential 참조 또는 Agent ID와 기존 클러스터 식별/연결 참조 | Provider별; 원문 제외 |
| 최종 이미지 | Digest 고정 URI, 지원 CPU 아키텍처, Registry 인증 참조 | 이미지 필수; 별도 Registry 인증은 필요한 경우 |
| 앱 실행 | Pod CPU·메모리 요청/제한, Replica, Port, Health Check | 공통; Node 사양과 구분 |
| Kubernetes 컴퓨팅 | 클러스터 요구, Node 수·유형/선정 정책, Node 아키텍처·디스크 | AWS/GCP; ON_PREM은 기존 자원 충족 여부 점검 |
| PostgreSQL | 버전 16, DB 이름·사용자, Pod 리소스, PVC 용량·스토리지 요구, 인증 생성 정책, 필요한 복제 준비 요구 | 공통; Node 루트 디스크와 PVC 분리; 데이터 복사·CDC 실행은 제외 |
| 환경변수 | `.env` 버전 참조, 키 분류·재매핑 규칙, 동적 DB 바인딩, Secret 참조/생성 정책 | 공통; DB 비밀번호는 기존 값을 무조건 복사하지 않음 |
| 네트워크 | P0 신규 네트워크의 주소·Subnet·아웃바운드·통신 요구 | AWS/GCP별 해석; 기존 네트워크 요청은 미지원, GCP에 AWS 모델 강제 금지 |
| 외부 접근 | Ingress/LB 요구, 서비스·대상 포트, Health Check | 대상별; ON_PREM 터널 제품을 확정하지 않음 |
| DNS/TLS | 도메인·인증서 참조 또는 발급 요구 | HTTPS 공개 시; 최종 DNS Cutover는 Traffic Migration |
| 온프레미스 | Agent·기존 K3s 연결, Namespace, StorageClass·가용 용량 요구 | ON_PREM; 새 K3s 생성은 제외; `local-path`는 확정 기본값 아님 |
| 이전 연결 준비 | 필요한 사설 통신 주체·경로·DB 포트 | 필요한 경우; WireGuard 구축·운영 전체를 Provisioning 소유로 간주하지 않음 |
| 객체 스토리지 | P0 자동 생성 입력으로 요구하지 않음 | 기존 S3 의존성은 진단에 보존; 처리 방안 미정이면 드러내며 조용히 무시하지 않음 |

`aws-ec2-basic`/`gcp-vm-basic`, VM Docker Volume 기반 DB 배포는 현행 대상과 맞지 않는다. Node Profile 예시를 사용하고 PostgreSQL은 StatefulSet·Service·PVC로 준비한다. 자동 선정된 Node 수·유형 및 적용 기본값도 Preview에서 확인할 수 있어야 한다.

## 5. Temporal 전달과 실제 저장

Temporal은 순서·Activity 결과 전달·재시도를 조정하며 별도의 `artifact://` 주소 체계를 제공하지 않는다. 작은 구조화 메타데이터는 Activity 결과로 직접 전달할 수 있고, 큰 파일은 S3에 저장한 후 위치/버전과 무결성 정보만 전달한다. 아래는 **필드 형태 예시**이며 실제 저장소나 기존 API가 아니다.

```json
{
  "contract_version": "1",
  "migration_id": "migration-001",
  "analysis_result": {
    "bucket": "example-platform-artifacts",
    "key": "migrations/migration-001/analysis/result.json",
    "version_id": "example-version-id",
    "sha256": "<64자리 SHA-256>"
  },
  "sandbox_result_id": "sandbox-result-001",
  "build_result_id": "build-result-001",
  "target_selection_id": "target-001",
  "target_selection_revision": 3,
  "environment_ref": {"id": "env-upload-001", "version": 2}
}
```

산출물 ID 방식을 택하면 Activity가 업무 기록에서 불변 저장 위치·버전·Digest를 해석한다. S3 직접 참조 방식을 택하면 버전 또는 덮어쓸 수 없는 객체와 Digest로 고정한다. `version_id`는 버전 관리를 사용하는 경우이며 최종 참조 모델은 팀 합의 대상이다. `latest`나 변경 가능한 ID만으로 계획 입력을 고정하지 않는다.

참조를 받았다는 사실은 접근 허용을 뜻하지 않는다. Activity는 해당 작업/소유권에 허용된 산출물인지, 버전·Digest가 일치하는지 검사한다. Workflow 안에서 S3·업무 DB·Secret을 조회하지 않는다. `.env` 원문·자격 증명·서명된 접근 URL을 일반 이력·Preview에 전달하지 않는다. Secret 참조에는 값에서 직접 계산한 공개 해시 대신 불투명 버전 식별자를 사용한다.

## 6. 계획 생성 전 검증과 오류

| 검사 | 실패 시 동작 |
| --- | --- |
| 필수 결과 누락, 지원하지 않는 계약 버전, 다른 작업의 결과 | 입력 출처·항목이 드러나는 오류. 승인 가능한 Preview 생성 금지 |
| Sandbox 미통과 또는 Build 실패 | 진행 차단. 누락된 검증·빌드 수행 필요 |
| Sandbox 검증 코드/설정과 Build 출처 불일치, 검증 Build 참조/이미지 Digest와 배포 이미지 불일치 | 계획 차단. 수정 시 새 빌드·재검증, 통과 후 재빌드 결과로 대체 금지 |
| Analysis 요구·변환 설정과 Sandbox 검증 근거 불일치 | 입력 불일치 반환. 임의로 최신 결과 선택 금지 |
| 검증 이후 대상·요구·환경설정 변경 | 영향 확인. 유효성을 입증하지 못하면 재검증 필요 상태; 세부 재검증 정책은 미정 |
| Node/이미지 아키텍처 불일치, 잘못된 CIDR·충돌, Provider 필수값 누락 | 항목별 오류. 정적 검사와 실제 대상 조회가 필요한 검사를 구분 |
| 사용자 선택이 필수 요구와 충돌 | 자동 덮어쓰기 대신 충돌 반환 |
| 참조 접근 거부·무결성 불일치 | 진행 차단. 민감값을 제외한 오류 기록 |

코드 Digest는 같은 산출물 범위끼리 비교한다. 빌드 설정, 런타임 매핑 템플릿, 환경별 동적 값은 별도 식별한다. Sandbox 임시 DB Endpoint와 운영 Endpoint의 값 자체가 같아야 한다고 검사하지 않는다. Sandbox 정리 실패의 후속 진행 정책은 아직 미정이며 검증 성공으로 덮어쓰지 않고 잔여 자원과 함께 보고한다.

## 7. 승인 후 실행 입력

실행에는 Plan ID·Revision, 승인 기록 참조, 고정된 Spec·이미지·Terraform Plan·Chart/Values/바인딩 규칙의 참조와 Digest, 대상/State 식별 정보를 연결한다. 상세 저장 형식은 [plan.md §8](plan.md)의 설계를 따른다. 단순 `approved: true`를 신뢰하거나 실행 Activity가 최신 계획을 조용히 재생성하지 않는다.

실행 시 승인 기록과 현재 Revision·대상·산출물 동일성을 재검증한다. 계획 변경은 새 Revision·Preview·승인으로 돌아간다. 이 경계의 구현은 티켓 07 및 각 실행 티켓에 속한다.

## 8. 결정 기록과 남은 합의

초기 #5는 입력 세 경계와 출처 추적을 정의했다. 후속 사용자 결정(#17, ADR-003)은 Build 선행·동일 Digest의 Sandbox 검증/운영 재사용을 필수로 변경한다. 새로운 HTTP 경로나 클라우드 제품은 확정하지 않는다. `ADR-001`의 스캐폴드 결정도 변경하지 않는다.

구현 전 또는 관련 티켓에서 합의할 사항:

- Analysis/Sandbox/Build 담당자의 실제 출력 필드, 상태 Enum, 계약 버전, 소스 패키징 및 Digest 산정 범위.
- 검증 요구/Profile 식별, 변경 시 재검증 범위, Sandbox 정리 실패 시 진행 정책.
- 동일 이미지 Digest 실행 검증은 필수다. 멀티 플랫폼 manifest의 개별 플랫폼 검증 증거와 Registry 보존/접근 정책은 B-01 착수 시 구체화한다.
- 산출물 ID 조회 또는 S3 직접 참조의 표준, 불변성·보관·접근 권한 정책.
- Node 선정 기본값, 신규 클러스터/네트워크 구성 범위(기존 네트워크는 P0 제외), Registry·Secret·TLS 정책 및 Agent 완료 통지.

## 9. 검증 범위

문서 변경은 제품 정책·PROV 요구사항·계획·책임 경계의 정합성과 상대 링크를 검토한다. 티켓 01의 코드 Test Seam은 선행 결과를 해석한 구조화 입력 → 공개 Planning 검증·조합 → 공개 Adapter 결과다. 이 검사는 실제 상위 Worker 성공·S3 권한·클라우드 배포를 증명하지 않는다. 코드 구현과 테스트 결과 없이 티켓 01을 DONE으로 표시하지 않는다.

## 10. 티켓 01 내부 v1 구현 (2026-11-03, #5)

**구현 전환 대기:** 아래는 이전 순서의 현재 코드 설명이다. `BuildResult.sandbox_ref`를 요구하고 SandboxResult에는 검증 이미지 Digest가 없다. 따라서 현재 `prepare_plan`의 성공은 P-10 동일 이미지 검증 정책의 충족 증거가 아니다. B-01(#17)에서 새 버전 계약·검증으로 전환하고 구 v1 입력을 자동으로 검증 완료 처리하지 않는다. 이번 PR #16의 후속 문서 변경은 런타임 모델/테스트를 변경하지 않는다.

사용자의 이슈 전체 구현 요청에 따라 위 설계에서 **로컬 조합·검증 경계**를 구현했다. 이는 다른 팀의 Activity 반환값을 강제로 바꾸는 wire 계약이 아니다. 외부 참조를 조회한 Activity가 아래 내부 모델로 정규화하여 호출한다. 아직 스캐폴드 Workflow에 연결하지 않았으며, 새 HTTP API도 추가하지 않았다.

| 공개 경계 | 구현 및 보장 |
| --- | --- |
| `cmp_contracts.provisioning.PlanningRequest` | `contract_version="1"`; Plan 식별 정보, 작업 ID, 해석된 Analysis/Sandbox/Build, 환경 참조와 대상 선택. 엄격한 타입과 추가 필드 금지 |
| `cmp_worker.provisioning.prepare_plan(dict)` | 누락/형식·선행 결과 상태·작업/버전/소스/설정 연결 검사 → `PlanningResult` |
| `cmp_providers.provisioning.adapt(ProvisioningSpec)` | 대상 아키텍처·네트워크 정적 검사와 EKS/GKE/K3s 구성 요약 반환. 직접 호출 시에도 대상 검증 수행 |
| `ProvisioningInputError.issues` | `code`, `path`, `message` 목록. 거부된 값·Pydantic 입력/context를 공개 오류에 포함하지 않음 |
| `PlanningResult` | `spec`, `configuration`, `pending_decisions`. Preview나 배포 성공 결과가 아님 |

내부 모델 세부 범위:

- `VersionRef`는 `id`와 문자열 `version`, `ContentRef`는 추가로 소문자 64자리 `sha256`을 갖는다. 조회·소유권·Digest 실제 계산은 이 함수 밖의 Activity 책임이다. 코드/설정 참조는 동일 산출물 범위끼리 비교하며, 환경별 임시 값과 Secret 원문은 비교 대상이 아니다.
- Analysis에는 코드·설정·요구사항 참조와 해석된 앱/DB 요구가 있다. Sandbox에는 Analysis·요구사항·환경·대상 선택 참조, 검증 코드/설정, 보고서 및 검증/정리 상태가 있다. Build에는 Sandbox 참조, 사용 코드/설정, 상태와 Digest 고정 이미지·플랫폼 목록이 있다.
- 검증 실패 또는 Build 실패는 진행 차단한다. 환경·대상 선택 버전이 달라지면 이 순수 함수는 유효성을 추정하지 않고 재검증이 필요한 입력 불일치로 반환한다. 더 세밀한 변경 영향 분석은 향후 정책이다.
- Sandbox 정리 `FAILED/PENDING`은 출처 정보에 보존하고 `CLEANUP_POLICY_REQUIRED`를 `pending_decisions`에 반환한다. 이 모델은 승인 가능한 Preview를 만들지 않는다. 후속 승인/실행 구현에서 미해결 정책을 처리해야 한다.
- AWS/GCP는 명시적 Node 수·인스턴스 유형·아키텍처·디스크, 위치·Credential 참조·네트워크를 받는다. 자동 Node 선정은 아직 미지원이며 조용한 기본값으로 대체하지 않는다. ON_PREM은 Agent·클러스터 참조·Namespace·StorageClass·아키텍처를 받는다.
- 최초 01에서는 `CREATE/EXISTING`을 구분했으나, 후속 [ADR-002](../../docs/decisions/ADR-002.md)에 따라 P0 공개 Planning/Adapter는 `EXISTING`을 `UNSUPPORTED_P0_NETWORK_MODE`로 거절한다. 기존 형태는 모델에서 읽을 수 있어도 지원을 뜻하지 않는다. AWS 신규 VPC는 CIDR과 Subnet AZ를, GCP Subnet은 Region을 요구하며 AWS VPC CIDR/AZ 필드를 GCP에 허용하지 않는다.
- CIDR은 canonical IPv4만 지원한다. Subnet 상호 중복·VPC 포함 관계와 전달된 점유 CIDR 충돌을 검사한다. `existing_cidrs`는 선택하여 재사용하는 Subnet을 제외한 외부/점유 범위다. 실제 클라우드 CIDR 조회·AZ 존재·인스턴스 카탈로그·가용 용량·접근 권한은 검증하지 않는다.
- 최소 앱 입력은 CPU·메모리 요구, Replica, Port, Health Path다. DB는 PostgreSQL 16·이름/사용자·CPU/메모리·PVC 용량과 `GENERATE` 인증 정책을 받는다. 리소스 limit, 복제 설정, TLS/Secret 저장 방식은 후속 Chart/실행 티켓에서 확장한다.
- 환경 매핑은 이번 버전에서 대상 DB의 host/port/name/username/secret_ref 바인딩만 구조화한다. 일반 `.env` 값은 환경 참조에 남기며 실제 조회·분류·주입은 티켓 03이다. P0 객체 스토리지 생성 필드는 미지원 오류로 반환한다.
- Provider 결과는 cluster/network/Ingress 종류, Node 사양, DB StatefulSet·PVC 용량, 앱 포트·이미지 요약이다. 완전한 Terraform 입력이나 Helm Values가 아니며 `execution_performed=false`를 명시한다. 승인 Plan 산출물·실행은 후속 티켓의 범위다.

### 재현 예시와 검증 증거

`tests/fixtures/provisioning/aws.json`, `gcp.json`, `on_prem.json`은 실제 비밀값·실자원 없이 같은 앱/DB 요구를 세 대상에 적용하는 독립 fixture다. 해당 경로는 저장소 루트 기준이다.

```sh
uv run pytest tests/test_provisioning.py -q
make check
```

RED에서 공개 진입점 부재, 검증 소스 불일치 미차단, CIDR 중복·아키텍처 불일치 미차단을 확인한 뒤 구현하여 GREEN으로 전환했다. 정리 실패는 정책 미정 결과를 보존하도록 별도 RED/GREEN 검증했다. 코드 테스트 경계는 사용자의 이슈 전체 완료 요청에 따라 위 공개 Planning/Adapter 경계로 확정했다. HTTP/Temporal 실행 코드는 수정하지 않았으므로 OpenAPI 재생성·Temporal smoke는 이번 변경 대상이 아니다.
