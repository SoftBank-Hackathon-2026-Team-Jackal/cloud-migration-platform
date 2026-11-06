# Ticket 23 — 환경 연결 관리 API (AWS 역할 위임·GCP·Agent 목록)

## 상태
DONE (로컬 범위) — [Issue #118](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/118). 수용 기준은 가짜 STS/EKS(botocore Stubber)·SQLite·TestClient로 확인했다. 실제 계정 연결과 데모 서버 반영은 사용자 승인 후 진행하는 미검증 항목이다(아래). 설계 결정: [ADR-005](../../docs/decisions/ADR-005.md). 자체 리뷰 완료, 조정 세션 검토 대기(push·PR 전).

## 목적
웹 `/settings` 연결 관리 화면에 필요한 백엔드를 만든다. 분석 스펙상 "원본 접근·대상 환경 연결 발급"은 Provisioning 담당이다. 그동안은 worker 호스트에 미리 설정한 프로필 이름만 썼다. 이 티켓으로 사용자가 키 없이 연결을 등록·확인하고, 발급된 `credential_ref`를 기존 분석·Provisioning 코드가 그대로 쓴다.

## 관련 요구사항
PROV-003, PROV-021, **PROV-022(신규, 환경 연결 발급)**

## 선행 작업
15(Agent 상태), 16(터널 상태), 21(데모 인스턴스 역할·AWS 프로필). 분석 AV-005 스캐너의 권한 목록(`ALLOWED_OPERATIONS`)을 읽기만 한다.

## 작업 범위
- 포함
  - `/connections` 등록·조회·역할 등록·확인(verify)·삭제
  - 통합 목록: 클라우드 연결 + Agent + 터널 상태
  - 기준점 조회: 리전별 EKS 클러스터
  - Agent 목록 `GET /agent-admin/agents`
  - `credential_ref` 생성 프로필
  - CloudFormation 원본·대상 역할 템플릿
  - Alembic 개정
  - ADR-005·SDD·OpenAPI 갱신
- 제외
  - `MigrationRequest` 계약 변경
  - 분석 스캔·범위 확정
  - GCP WIF 구현
  - Agent 버전 보고(`version`은 `null`)
  - 네임스페이스·워크로드 조회
  - 데모 서버 반영
  - 실제 계정 검증

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| C1 AWS 방식 | 계정 간 AssumeRole + 플랫폼 발급 External ID(`cmp-` + 32 hex, 연결마다 고유, 한 번 발급). 사용자는 Launch Stack으로 역할을 만들고 Role ARN만 등록(한 번만, 같은 값 재등록은 멱등, 다른 값은 `ROLE_ALREADY_SET`) | 사용자 결정. 키 입력·External ID 없는 위임은 ADR-005에서 기각 |
| C2 `credential_ref` | 생성된 AWS config 파일을 공유한다. 역할이 등록된 연결마다 `[profile cmp-conn-<hex>]`(`role_arn`, `external_id`, `source_profile=<플랫폼 프로필>`, `duration_seconds=3600`, `region`)를 둔다. API가 DB 기준으로 원자적으로 다시 쓰고, 앞에 플랫폼 기본 프로필 파일을 붙인다. worker는 `AWS_CONFIG_FILE`로 읽는다 | 분석 `get_session`·Provisioning `aws_profile`·Terraform·CLI가 코드 변경 없이 동작한다. SDK가 임시 자격 증명을 갱신한다. 코드 수준 AssumeRole만으로는 하위 프로세스에 키를 넘겨야 해서 기각 |
| C3 verify 경로 | API 프로세스에서 코드로 AssumeRole(900초, ExternalId) → 임시 자격 증명으로 GetCallerIdentity → 원본이면 등록 리전 `eks:ListClusters`(페이지 전부). 결과는 HTTP 200 + `CONNECTED`/`FAILED` + 고정 코드 | 오류를 구조화하려면 직접 호출해야 한다. 짧은 확인이라 Workflow를 거치지 않는다. 임시 자격 증명이 Temporal 이력에 들어가지 않는다 |
| C4 오류 코드 | `ROLE_NOT_SET`, `ASSUME_ROLE_DENIED`, `ASSUME_ROLE_FAILED`, `PLATFORM_CREDENTIALS_UNAVAILABLE`, `IDENTITY_CHECK_FAILED`, `ACCOUNT_MISMATCH`, `EKS_LIST_DENIED`, `EKS_LIST_FAILED`, GCP는 ADC 확인 코드 그대로. `EXTERNAL_ID_MISMATCH`는 내지 않는다 | AWS는 External ID 불일치와 신뢰 없음을 같은 AccessDenied로 응답해 구별할 수 없다(웹 계약 예시와 다름, 아래 확인 필요) |
| C5 GCP | 데모는 `connection_gcp_project_id` 고정. 다른 프로젝트·키 JSON·추가 필드는 422. 확인은 `cmp_providers.gcp_preview.verify_adc_principal`(Planning의 ADC 검사 공개 함수) | 사용자 결정. WIF는 ADR-005에 설계만 |
| C6 범위 | 기준점 조회만 한다(`GET /connections/{id}/eks-clusters?region=`, `AWS_SOURCE` 전용). 참조 추적·범위 확정은 분석 담당. 네임스페이스·워크로드는 EKS access entry가 필요해 후속 | 계정 전체 스캔 금지(사용자 결정) |
| C7 원본 권한 | 템플릿 허용 동작 = `required_iam_actions()` + `eks:ListClusters`. 테스트가 정확히 같음을 강제한다 | 분석 스캔 목록에서 파생. 분석 담당 확인 대기 |
| C8 대상 권한 | 생성은 요청 태그 `cmp:managed-by=terraform`, 변경·삭제는 같은 리소스 태그, IAM 쓰기는 `ResourcePrefix`(기본 `cmp-`) 이름과 EKS OIDC만. PassRole은 eks/ec2로만. IAM 사용자·키 생성과 자기 역할 변경은 명시적 거부 | 권한 경계는 Terraform 역할 변경이 필요해 후속. 대상 클러스터 이름이 `cmp-`로 시작해야 IAM 이름이 맞는다 |
| C9 템플릿 호스팅 | 플랫폼 계정 S3 버킷에 `connections/v1/{source,target}-role.yaml`을 공개 읽기로 둔다. 접두어를 `connection_template_base_url`로 설정한다. 바꾸면 `v2`로 올린다. 미설정이면 `launch_stack_url: null` | 빠른 생성은 S3 HTTPS URL만 받는다. 버킷 생성·업로드는 승인 대상 |
| C10 인증·노출 | 모든 라우트에 운영자 키 `X-Enrollment-Admin-Key`(Agent 관리와 동일), CORS 없음. External ID는 생성 응답·상세에만 나오고 목록에는 없다. 422는 입력값을 되돌리지 않는다(라우터 전용 `InputHidingRoute`) | 웹 BFF 서버 측 호출 전용 |
| C11 Agent 목록 | `online` = 폐기되지 않았고 `now - last_seen_at ≤ 60s`. `tunnel`은 마지막 `agent_tunnel_status` 보고(16), 없으면 `null`. `version: null` | Agent 런타임(17 진행 중)은 건드리지 않는다 |
| C12 시각 표현 | `checked_at`·`last_verified_at`·`last_seen_at`·`last_handshake_at`은 epoch 초 정수 | 기존 Agent·터널 API와 같다 |
| C13 삭제 | 플랫폼 데이터와 생성 프로필만 지운다. 응답에 `stack_name`과 사용자 계정 스택 삭제 안내 | 사용자 계정 리소스는 플랫폼이 지우지 않는다 |

## API 계약
[shared-contracts.md "환경 연결 (23)"](../../specs/provisioning/shared-contracts.md)과 `docs/contracts/openapi.yaml`.

## 검증 기록
- `tests/test_environment_connections.py` (52)
  - 운영자 키 없거나 틀리면 8개 라우트 모두 403, 키 미설정 시에도 403
  - AWS 생성: External ID·빠른 생성 링크(템플릿·매개변수 인코딩), 대상은 대상 템플릿, External ID 고유, 템플릿 미설정 시 링크 `null`
  - 키·`role_arn`·`external_id`를 생성 요청에 넣으면 422, 입력값을 되돌리지 않음. 잘못된 리전 422
  - Role ARN 형식 검증(줄바꿈 주입 포함), 한 번만 등록, GCP에 역할 등록 거절
  - verify: 역할 없음 `ROLE_NOT_SET`(STS 미호출). 성공 시 정확한 AssumeRole 매개변수(ExternalId·900초), 임시 자격 증명으로 STS·EKS 호출, 페이지 넘김 클러스터 목록, 상세 상태 반영. 대상은 클러스터 조회 생략
  - 실패 코드 `ASSUME_ROLE_DENIED`/`ASSUME_ROLE_FAILED`/`ACCOUNT_MISMATCH`/`EKS_LIST_DENIED`/`PLATFORM_CREDENTIALS_UNAVAILABLE`
  - 임시 자격 증명 값이 응답·DB 행·로그에 없음
  - 기준점 조회: 다른 리전 지정, 원본 전용, 역할 필요, 리전 형식
  - GCP: ADC 주체 확인 성공·불일치, 다른 프로젝트·누락·키 JSON·잘못된 이메일 422, AWS에 gcp 필드 422
  - 통합 목록이 웹 계약 형태와 정확히 같음(연결 2종, Agent online/stale, 터널 있음/없음), `/agent-admin/agents` 동일 항목
  - 삭제: 안내·스택 이름, 이후 404
  - 기본 GCP 확인 함수가 Planning ADC 검사를 호출하고 실패 코드를 그대로 옮김
  - `credential_ref`: 생성 config에 플랫폼 프로필 + assume-role 프로필, 키 없음. `AWS_CONFIG_FILE`로 분석 `get_session`이 같은 프로필(role_arn·external_id)을 해석함. 삭제 시 프로필 제거. 대기·GCP 연결은 `credential_ref: null`. External ID는 목록에 없음
- `tests/test_connection_templates.py` (4): 두 템플릿 신뢰 정책이 플랫폼 주체 + `sts:ExternalId` `StringEquals` 하나뿐, 원본 허용 동작이 스캐너 목록 + `eks:ListClusters`와 정확히 같고 쓰기 없음, 대상 쓰기 범위(태그·이름 접두어·PassRole·거부 문). `cfn-lint`는 설치되어 있지 않아 구조 테스트로 대신했다.
- Alembic `20261105_05`: SQLite에서 upgrade → `alembic check`(No new upgrade operations) → downgrade → upgrade.
- `make check`, `make openapi`(7개 경로 추가만, 기존 경로 변화 없음).

## 미검증 (사용자 승인 필요)
- 실제 계정에서 스택 생성(두 템플릿의 CloudFormation 실제 검증 포함) → Role ARN 등록 → verify → 분석 스캔 → 대상 Plan/Apply. 대상 역할 권한이 Terraform 전체 Apply에 충분한지는 실제 실행으로만 확인된다.
- 데모 반영(원격):
  - 인스턴스 역할에 `sts:AssumeRole` 추가
  - API 컨테이너에 플랫폼 프로필·`CONNECTION_*` 설정
  - api·worker가 생성 config를 공유하고 worker `AWS_CONFIG_FILE`이 그 파일을 가리키게 함
  - 템플릿 S3 버킷 생성·업로드
- GCP verify의 실제 ADC 호출은 기존 06/10 경로와 같은 함수지만, API 컨테이너에는 gcloud·ADC가 없다(데모는 worker에만 있음). 데모 반영 시 API에 ADC를 줄지, 확인을 worker로 옮길지 결정이 필요하다.

## 후속 제안
- `MigrationRequest`에 `source_connection_id`/`target_connection_id`를 싣고 Workflow가 `credential_ref`로 바꿔 분석·Provisioning에 넘긴다. Migration 담당과 합의해야 한다.
- 다른 계정 대상 Apply 때 Terraform S3 backend에 플랫폼 프로필을 따로 준다(`-backend-config=profile=`).
- 대상 역할 권한 경계와 Terraform 역할의 `permissions_boundary`
- 분석 세션 정책으로 선택 리전·클러스터 축소
- 네임스페이스·워크로드 기준점(EKS access entry)
- 리전 목록 조회
- GCP WIF
- Agent 버전 보고

## 확인 필요
- 웹 계약 예시의 `EXTERNAL_ID_MISMATCH`는 AWS가 구별해 주지 않는다. 웹은 `ASSUME_ROLE_DENIED`에서 "External ID·플랫폼 주체 확인" 안내를 보여 줘야 한다.
- 시각 필드는 epoch 초 정수다. 웹 목업이 ISO 문자열이면 BFF에서 변환하거나 계약을 맞춰야 한다.

## 후속 (티켓 46, 2026-11-06)
- Provisioning이 연결 `credential_ref`로 Plan·Apply·워크로드를 실행하고, State backend는 플랫폼 프로필을 따로 쓴다. 플랫폼 주체는 고정 역할 `cmp-platform-connector`(스택 `cmp-demo-connector`), 템플릿 역할 이름은 `cmp-<종류>-<External ID hex>`, 생성 파일은 API 시작 때도 다시 쓴다. 위 "남는 일"의 backend profile·AssumeRole 권한·compose 연결 항목은 46에서 처리했다(배포는 승인 대기). [46](46-connection-credentials.md)
