# Ticket 19 — DB 연결 URL Secret 출력 (D-16)

## 상태
DONE — [Issue #79](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/79). 자체 리뷰 완료, PR 리뷰 대기. 2026-11-05 팀 합의(D-16, [analysis tasks](../../specs/analysis-verification/tasks.md))와 사용자 지시로 바로 착수.

## 목적
`DATABASE_URL` 하나로 DB에 연결하는 앱(데모 앱 등)을 지원한다. 필드별 출력(host·port·name·username·password_secret_ref)만으로는 표현할 수 없어서, 플랫폼이 접속 URL을 만들어 Secret에 넣고 앱에는 Secret 이름·키만 주입한다. 자유 형식 템플릿은 쓰지 않는다.

```yaml
env_bindings:
  DATABASE_URL:
    resource_id: postgres-main
    output: connection_url_secret_ref
```

## 관련 요구사항
PROV-004, PROV-005, PROV-010, PROV-012, PROV-021

## 선행 작업
Blocked by: 01A, 04B, 09, 11, 12 (DONE). 후속: 분석 07(바인딩 생성), 11(Sandbox에서 같은 Secret 생성·검증) — 승철님.

## 작업 범위
- 포함
  - 입력 계약 v2 `ResourceOutputBinding.output`에 `connection_url_secret_ref` 추가, `DatabaseOutputsV2.connection_url_secret_ref`
  - v2 공통 렌더러: 바인딩을 URL Secret 참조로 해석, 이름 충돌 검사
  - v1 앱 Values(AWS/GCP 운영 배포 09·11 경로): `DatabaseBinding.field`에 같은 출력 추가, 동적 바인딩·Chart 주입
  - 공통 URL 생성 함수(Sandbox 11이 재사용)
  - Secret 생성: ON_PREM Agent(12), AWS/GCP 배포 Activity(09·11)
  - SDD·OpenAPI(해당 시)·입력 계약 문서 갱신
- 제외: Sandbox의 Secret 생성 구현(분석 11), 분석 07의 바인딩 생성, DSN(key=value) 형식, TLS·쿼리 파라미터, 자유 형식 템플릿

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| U1 출력 이름 | v2 `output: connection_url_secret_ref`, v1 `DatabaseBinding.field: connection_url_secret_ref` | D-16 합의 |
| U2 Secret 이름·키 | 이름 `<DB release>-url`, 키 `url`. DB release는 53자 이하라 이름은 57자 이하. 플랫폼 관리 라벨을 붙이고, 같은 이름의 관리 밖 Secret이 있으면 덮어쓰지 않고 `SECRET_NAME_CONFLICT`. 비밀번호 Secret 이름과 같으면 렌더 단계에서 거절 | 사용자 Secret을 덮어쓰지 않는다. 비밀번호 Secret에 키를 추가하는 방식은 사용자가 미리 만든 Secret을 수정해야 해서 피했다 |
| U3 URL 형식 | `postgresql://<user>:<password>@<host>:5432/<db>`. 사용자·비밀번호·DB 이름은 RFC 3986 퍼센트 인코딩(`quote(safe="")`), host는 `<DB release>.<namespace>.svc`, 쿼리 파라미터 없음(클러스터 내부, TLS 없음). 공통 함수 `cmp_providers.database_url` | D-16의 "형식은 민서님과 확정" 항목. Sandbox·운영이 같은 함수를 써서 형식이 갈라지지 않게 한다 |
| U4 생성 주체·시점 | DB 비밀번호 Secret을 다루는 실행 주체가 배포 때마다 만들거나 갱신한다: ON_PREM은 Agent(클러스터 안에서 비밀번호를 읽어 조립), AWS/GCP는 배포 Activity. 앱이 이 출력을 참조할 때만 만든다 | 비밀번호를 아는 쪽만 URL을 만들 수 있다. 매번 갱신해 비밀번호와 어긋나지 않게 한다 |
| U5 비밀 보호 | URL 값은 Values·Plan·Preview·작업 본문·로그·Temporal 이력에 넣지 않고 Secret 참조만 남긴다. Secret은 stdin으로만 전달한다 | PROV-021 |
| U6 AWS/GCP 비밀번호 읽기 | 비밀번호 Secret이 이미 있으면(재실행·사용자 제공) Activity가 그 값을 메모리로 읽어 URL을 조립한다. 값은 반환·기록하지 않는다 | 새로 만들 때만 URL을 만들면, 출력이 나중에 추가된 앱이나 사용자 제공 비밀번호를 지원하지 못한다 |

## 수용 기준
- [x] v2 입력에서 `connection_url_secret_ref` 바인딩이 앱 env의 Secret 참조(`<release>-url`/`url`)로 렌더되고, 알 수 없는 출력·이름 충돌은 구조화 오류다.
- [x] v1 앱 Values에서도 같은 출력이 Chart의 `secretKeyRef`로 주입된다.
- [x] 공통 함수가 특수문자가 든 사용자·비밀번호·DB 이름을 인코딩한 URL을 만든다.
- [x] ON_PREM Agent와 AWS/GCP 배포가 앱이 참조할 때 URL Secret을 만들거나 갱신하고, 관리 밖 같은 이름 Secret은 덮어쓰지 않는다.
- [x] URL·비밀번호 값이 Values·Preview·작업 본문·결과·로그에 없다.
- [x] 기존 필드별 바인딩과 09·11·12 경로 회귀 없음.

## Test Seam
- 순수 함수: URL 생성·인코딩, v2 렌더러 결과(실제 Helm 렌더), v1 `prepare_application`·`resolve_application`과 Chart 렌더.
- ON_PREM: 12의 가짜 클러스터로 Agent의 Secret 생성·갱신·충돌·검증.
- AWS/GCP: 09·11의 가짜 ClusterSession으로 배포 Activity의 Secret 생성·갱신·충돌.
- 놓치는 것: 실제 클러스터에서 데모 앱이 URL로 접속하는지(분석 11·통합 검증에서 확인).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰; PR 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `make check` (ruff + pytest, Helm v4.3.1) | 771 passed, 1 skipped |
| `uv run pytest tests/test_database_url.py` | URL 형식·퍼센트 인코딩 왕복·잘못된 입력 거절·Secret 이름 규칙, v2 렌더(실제 Helm `secretKeyRef`, URL 문자열 미포함)·이름 충돌 2종·알 수 없는 출력, v1 동적 슬롯·Chart 주입·사전 채움 거절·기존 Fixture 불변 |
| `uv run pytest tests/test_database_url_rollout.py` | ON_PREM 작업 본문의 `connection_url`(사용자 Secret 목록 제외), Agent의 생성·비밀번호 재사용·오래된 URL 갱신·관리 밖 Secret 거절·미사용 시 미생성(작업 바이트 불변), Preview 표시 |
| `uv run pytest tests/test_database_url_workloads.py` | AWS/GCP 공통 배포 Activity의 생성·기존 비밀번호 재사용·관리 밖 Secret이면 변경 전 실패·미사용 시 미생성, 결과·trace·로그에 비밀번호·URL 없음 |
| `make openapi` | Agent claim 응답의 `ProductionRolloutJob`에 선택 필드 `database.connection_url`(`RolloutConnectionUrl`) 추가. 검토 완료 |

구현 메모: 실패 테스트를 먼저 작성했다. v1 `DatabaseConnection.url_secret_name`과 작업 본문 `connection_url`은 비어 있으면 직렬화에서 빠지게 해(`exclude_if`), 이미 승인된 Values·대기 중인 작업의 바이트와 Digest가 바뀌지 않는다. 앱 Chart 스키마는 모델에서 다시 생성했다. 테스트 지원 코드에 `url_binding`(ON_PREM)·`rules_from`(AWS) 선택 인수를 추가했다.

### 미검증·제한 사항
- 실제 클러스터에서 데모 앱이 `DATABASE_URL`로 접속하는지는 확인하지 않았다(분석 11 Sandbox 검증·통합 검증 범위). 렌더·실행 주체 동작은 실제 Helm 렌더와 가짜 클러스터로 확인했다.
- 이미 설치된 온프레미스 Agent는 새 버전으로 업그레이드해야 URL Secret을 만든다(15 릴리스 경로). 이전 Agent는 `connection_url` 필드를 모르는 작업을 `UNSUPPORTED_JOB`으로 거절한다.
- Sandbox의 URL Secret 생성(분석 11)과 분석 07의 바인딩 생성은 이 티켓 범위 밖이다. 같은 함수 `cmp_providers.database_url.connection_url`·`connection_url_secret_name`을 쓰면 운영과 같은 형식이 된다.
- `spec.md` 요구사항은 바뀌지 않았다. 입력 계약 v2·공통 배포 계약·공유 계약·앱 렌더 문서와 OpenAPI를 갱신했다.
