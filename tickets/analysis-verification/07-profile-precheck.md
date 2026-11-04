# Ticket 07 — 요구사항·사용 프로필·사전 점검 조립

## 상태
IN_PROGRESS — 구현 완료, 리뷰 대기. Issue 번호는 발행 후 기입

## 목적
스캔 결과를 `RequirementsV2` 요구사항과 사용 프로필로 합치고, 환경변수 예외 목록·코드 수준 사전 점검을 만든다.

## 관련 요구사항
AV-006, AV-017

## 선행 작업
Blocked by: 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시)

## 작업 범위
- 포함: 결과 병합, 교차 해석(코드·CloudTrail·권한), `RequirementsV2` 생성(`resources[]`, DB 연결 `env_bindings`, `network_intent`), 확장 요구 시 `BLOCK` 보고, `EnvExceptions`(제외·대체·누락), Dockerfile·빌드 컨텍스트 점검, 코드 수준 사전 점검
- 제외: 매핑 판단(08), DB 내부 상태 점검(D-13)

## 수용 기준
- [x] 요구사항에 `APPLICATION`·`POSTGRESQL` 리소스와 DB 연결 `env_bindings`가 나오고 `RequirementsV2` 검증을 통과한다.
- [x] 앱이 PostgreSQL 확장을 요구하면 요구사항은 비워 두고 해당 기능이 `BLOCK`으로 보고된다.
- [x] 원본 환경에 묶인 변수(`AWS_REGION` 등)가 `excluded`로, 누락 변수가 `missing`으로 나온다.
- [x] Dockerfile 유무·위치와 누락된 `COPY` 경로가 기록된다.
- [x] 세션 단위 기능·확장 사용·헬스체크 경로·노출된 클라우드 주소가 사전 점검에 나온다.

## 테스트 전략
- 합의된 Test Seam: `build_requirements(...)`·`precheck(...)` 결과. 잡는 것: 병합·연결·분류 규칙. 못 잡는 것: 실제 원본 DB 상태.
- 실행: fixture, `uv run pytest tests/analysis/test_requirements.py`

## 완료 시 확인할 동작
스캔 fixture로 완성된 요구사항·`AnalysisReport`(매핑 제외)·예외 목록이 나온다.

## 검증 증거
- `uv run pytest tests/analysis/test_requirements.py`: 14 통과. 매니페스트·코드 스캔 fixture로 `RequirementsV2` 검증 통과, `APPLICATION`·`POSTGRESQL` 리소스와 필드별 DB `env_bindings`, 앱→DB 5432 `network_intent`, 확장 요구 시 요구사항 없음과 `BLOCK` 보고, 지원하지 않는 PostgreSQL 버전 차단, 선언되지 않은 값의 이름 있는 기본값과 `assumptions` 기록, 요구사항 직렬화·해시 안정성, `EnvExceptions`(제외·대체·누락), Dockerfile 유무·위치·누락 `COPY` 경로(여러 줄·JSON 배열·`--from`·URL·변수 제외), 사전 점검(확장 사용·헬스체크 경로 없음·`*.amazonaws.com` 주소·세션 라이브러리)을 확인했다.
- 04 매니페스트 스캔에 07이 필요한 정보를 추가했다: DB 이름·사용자 리터럴(`POSTGRES_DB`·`POSTGRES_USER` 등 허용 목록만, 비밀 값 제외)과 PVC 용량(`volume_claims`). `test_manifests.py` 14 통과.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 전체 통과, `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 실제 데모 앱 확인: `demo-app`의 Dockerfile·Go 소스·`db/schema.sql`을 임시 폴더에 받아 점검했다(저장소에는 복사하지 않음). Dockerfile은 루트에서 찾았고 누락된 `COPY` 경로는 없었으며 확장 생성은 없었다. 이 저장소에는 k8s 매니페스트가 없어 요구사항 조립은 `NO_APPLICATION_WORKLOAD`로 보고됐다.

## 계약 공백 (민서님 확인 필요)
- `RequirementsV2`의 `env_bindings`는 `host`·`port`·`name`·`username`·`password_secret_ref` 필드별 출력만 표현한다. 데모 앱은 `DATABASE_URL` 단일 변수로 연결하므로 바인딩으로 표현할 수 없다. 구현은 이 경우 바인딩을 만들지 않고 `unmapped_items`에 `DB_CONNECTION_URL_VARIABLE`로 보고한다. URL 조립을 Sandbox 워커(11)가 맡을지, 계약에 URL 출력을 추가할지는 정하지 않았다.

## 한계와 미검증
- 선언되지 않은 값(replica·CPU·메모리·포트·헬스 경로·DB 이름·사용자·저장 용량)은 이름 있는 기본값을 쓰고 `assumptions`에 기록한다. 기본값 자체가 정책적으로 맞는지는 검토가 필요하다.
- 첫 번째 컨테이너를 앱으로 보고 모든 `APPLICATION`에 같은 DB 바인딩을 붙인다. DB가 여럿이면 첫 번째에만 바인딩하고 보고한다.
- 외부 호스트 `network_intent`는 06(네트워크 스캔)에서 만든다. 07은 앱→DB intent만 낸다. 06 결과가 없는 인프라 항목은 "미수집"으로 둔다.
- 헬스 체크 경로는 매니페스트 probe에서만 찾는다(코드의 `/healthz` 라우트는 보지 않는다). 세션 탐지는 라이브러리 이름 기준이다. 클라우드 주소는 `amazonaws.com` 호스트만 본다.
- 02의 `build_requirements`·`precheck` Activity는 mock 그대로이며 연결은 후속에서 한다.

## 보강 (Issue #75)
- NetworkPolicy 규칙의 라벨 선택자를 워크로드와 맞춰 앱 → DB 같은 `network_intent`를 만든다(코드 스캔 결과 없이도 나오고, DB 드라이버에서 나온 의도와 중복되지 않는다).
- ConfigMap 키와 `secretKeyRef`로 주입되는 변수 이름을 선언된 변수로 보아 `.env`가 없는 배포에서도 `missing`·`replaced`가 맞게 나온다.
- 앱 코드에 DB 드라이버가 있는데 클러스터 안 PostgreSQL Pod가 없으면 `DB_NOT_IN_CLUSTER`로 지원 범위 밖임을 보고한다(첫 사이클은 RDS 없이 Pod로 결정).
- 실제 원본 매니페스트로 확인했다: 앱 → DB 5432 의도가 나오고 DB 변수 5개가 바인딩으로 대체되며 누락은 없다.

## 후속 (D-16, 민서님 구현 대기)
- 단일 URL 연결 변수(`DATABASE_URL` 등)는 V2에 `connection_url_secret_ref` 출력이 추가된 뒤 그 출력으로 바인딩한다. 지금은 `DB_CONNECTION_URL_VARIABLE`로 표현 불가를 보고한다. 병합 후 `_bindings`가 `*_URL`·`*_URI` 변수를 새 출력으로 바인딩하고 `replaced`에 넣으며, DSN 형식은 계속 보고만 한다. Sandbox(11)는 같은 방식으로 URL Secret을 만들어 앱 연결을 검증한다.

## 결정 대기
- 확장 지원(D-14)
- DB 내부 상태 점검 담당(D-13)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리소스 목록 요구사항, 예외 목록 반영. DB 내부 상태 점검은 D-13으로 분리. v2 동기화로 요구사항을 `RequirementsV2`로 바꾸고 DB 이미지 확정 규칙 삭제(이미지 선택은 D-11).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
