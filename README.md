# Cloud Migration Platform — 백엔드 세팅

클라우드 마이그레이션의 API, Temporal Workflow, 공통 계약, 인프라 템플릿을 한 저장소에서 관리한다. **프론트엔드는 포함하지 않는다.** 현재 코드는 팀이 Workflow → Activity → 승인 Signal → 재개를 로컬에서 확인하기 위한 뼈대다. 클라우드 스캔, 이미지 빌드, Terraform 적용, 데이터 복사, 트래픽 전환은 실행하지 않으며 결과에 `mode: scaffold`를 표시한다.

## 시작하기

필요한 도구: Python 3.11, [uv](https://docs.astral.sh/uv/), Docker Compose. 로컬 포트는 API `8000`, Temporal `7233`, Temporal UI `8080`, 업무 PostgreSQL `5433`이다.

```bash
cp .env.example .env
make setup
make infra-up
```

별도 터미널에서 `make workers`, `make api`를 실행한다. API 문서는 `http://localhost:8000/docs`, Temporal UI는 `http://localhost:8080`이다. Docker가 시작되지 않았다면 두 프로세스도 Temporal에 연결되지 않는다.

Temporal만 빠르게 확인할 때는 `make infra-up` 후 `make smoke-temporal`을 실행한다. 이 명령은 테스트 Worker를 잠시 실행하고 승인 대기·재개·완료를 확인한다. **Task Queue 경계와 Worker 실행 방법은 [Temporal 사용 가이드](docs/architecture/temporal-flow.md)**에 정리했다.

```bash
curl -X POST http://localhost:8000/migrations \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"demo"}'
WORKFLOW_ID='첫 응답의 workflow_id'
curl "http://localhost:8000/migrations/$WORKFLOW_ID"
curl -X POST "http://localhost:8000/migrations/$WORKFLOW_ID/approve"
curl -N "http://localhost:8000/migrations/$WORKFLOW_ID/events"
```

승인 요청은 `phase`가 `awaiting_approval`인 것을 확인한 후 보낸다. 취소 예제는 `/approve` 대신 `/cancel`을 호출한다. 종료할 때 `make infra-down`을 사용한다. 이는 볼륨을 삭제하지 않는다.

## 프로젝트 구조와 파일별 역할

| 경로 | 역할 |
| --- | --- |
| `AGENTS.md` | AI와 팀원이 따를 문서 우선순위, 코드 경계, 검증 규칙 |
| `README.md` | 저장소의 현재 범위, 설치, 실행, 전체 경로 안내 |
| `pyproject.toml` | Python 3.11 범위, 런타임·개발 의존성, 네 패키지의 설치 경로, 도구 설정 |
| `.python-version` | uv가 선택할 Python 버전 |
| `.env.example` | 로컬 환경변수의 이름과 개발용 기본값; 실제 `.env`는 Git 제외 |
| `.gitignore` | 가상환경, 비밀값, Terraform state·plan, 빌드 산출물 제외 |
| `Makefile` | `setup`, `infra-up/down`, `api`, `workers`, `worker`, `lint`, `test`, `check`, `openapi`, `smoke-temporal` 공통 명령 |
| `alembic.ini` | 업무 PostgreSQL 스키마 변경 도구의 설정 |
| `uv.lock` | 팀과 CI가 동일한 Python 의존성 버전을 설치하도록 고정 |
| `scripts/export_openapi.py` | 현재 FastAPI 라우트에서 OpenAPI YAML을 생성 (`make openapi`) |
| `scripts/smoke_temporal.py` | 실제 로컬 Temporal에서 승인 대기·재개 흐름 확인 |
| `apps/api/src/cmp_api/main.py` | FastAPI 앱과 헬스 체크, 라우터 등록 |
| `apps/api/src/cmp_api/routes/migrations.py` | 예제 마이그레이션 시작·상태·승인·취소·SSE API |
| `apps/api/src/cmp_api/temporal_client.py` | Temporal 연결 설정을 읽는 클라이언트 진입점 |
| `apps/api/src/cmp_api/config.py` | API용 Temporal 주소·Namespace·업무 DB 연결 설정 |
| `apps/api/src/cmp_api/db.py` | 업무 DB의 SQLAlchemy Base·세션 생성기 |
| `apps/api/migrations/env.py` | Alembic이 업무 DB와 모델 메타데이터를 읽는 진입점 |
| `apps/api/migrations/script.py.mako` | 새 DB 변경 파일의 생성 서식 |
| `apps/api/migrations/versions/README.md` | DB 변경 파일 작성·적용 안내; 현재 테이블 없음 |
| `apps/worker/src/cmp_worker/config.py` | Worker용 Temporal 주소·Namespace 설정 |
| `apps/worker/src/cmp_worker/workflows/migration.py` | 단계 순서, 승인 대기, 취소 Signal, 상태 Query, Activity 재시도 |
| `apps/worker/src/cmp_worker/activities/analysis.py` | 소스·인프라 분석 Activity의 연결 자리; 현재 스캔 없음 |
| `apps/worker/src/cmp_worker/activities/build.py` | AI 수정·샌드박스·이미지 준비 연결 자리; 현재 빌드 없음 |
| `apps/worker/src/cmp_worker/activities/provisioning.py` | 계획과 대상 인프라 적용 연결 자리; 현재 Plan/Apply 없음 |
| `apps/worker/src/cmp_worker/activities/migration.py` | 데이터 복사·트래픽·검증 연결 자리; 현재 외부 변경 없음 |
| `apps/worker/src/cmp_worker/workers/analysis.py` | `analysis` Task Queue 등록 |
| `apps/worker/src/cmp_worker/workers/build.py` | `build` Task Queue 등록 |
| `apps/worker/src/cmp_worker/workers/provisioning.py` | `provisioning` Task Queue 등록 |
| `apps/worker/src/cmp_worker/workers/migration.py` | Workflow용 `migration`, Activity용 `migration-activities` Queue 등록 |
| `apps/worker/src/cmp_worker/workers/run_all.py` | 로컬에서 다섯 Worker를 한 프로세스로 실행 |
| `apps/worker/src/cmp_worker/workers/run.py` | `make worker QUEUE=<경계>`로 한 Worker만 실행 |
| `packages/contracts/src/cmp_contracts/migration.py` | Pydantic 요청·응답·단계 결과 모델 |
| `packages/contracts/src/cmp_contracts/queues.py` | API와 Worker가 함께 사용하는 Workflow·Task Queue 이름 |
| `packages/providers/src/cmp_providers/__init__.py` | 공급자별 변수 변환 코드가 들어갈 패키지 경계 |
| `infra/local/compose.yaml` | 업무 PostgreSQL과 Temporal 전용 PostgreSQL, Temporal Server·UI |
| `infra/terraform/README.md` | 향후 AWS/GCP 모듈과 State 운영 규칙 |
| `infra/helm/README.md` | 향후 K3s 워크로드 Chart의 위치와 범위 |
| `tests/test_scaffold.py` | 기본 계약과 API 헬스 체크 검증 |
| `.github/workflows/check.yml` | PR에서 Python 형식 검사와 테스트 실행 |

각 패키지의 `__init__.py`는 해당 디렉터리를 Python 패키지로 선언한다. `apps/api`와 `apps/worker`는 각각 실행 진입점이며, `packages/contracts`와 `packages/providers`는 공유 코드다. 로컬에서는 하나의 `pyproject.toml`로 함께 설치하고, Worker는 Task Queue별로 나뉘어 있어 이후 별도 프로세스로 배포할 수 있다.

### 문서와 기능 명세

| 경로 | 역할 |
| --- | --- |
| `docs/product/overview.md` | 제품 목적, 기본 시나리오, 현재 구현 범위 |
| `docs/product/policies.md` | 이 저장소의 확정 제품 정책 기준과 제안서와의 차이 |
| `docs/architecture/overview.md` | 실행 구조와 모듈 의존 관계 |
| `docs/architecture/domain-boundaries.md` | 각 모듈의 책임 및 금지 경계 |
| `docs/architecture/temporal-flow.md` | Temporal 개념, 로컬 실습, 실제 구현 시 규칙 |
| `docs/contracts/openapi.yaml` | 현재 구현된 HTTP API의 명시적 계약 |
| `docs/decisions/ADR-001.md` | Temporal 뼈대를 채택한 이유와 범위 |
| `specs/_template/spec.md` | 기능 요구사항·수용 기준 서식 |
| `specs/_template/plan.md` | 구현 설계·영향 범위 서식 |
| `specs/_template/tasks.md` | 작업 목록·완료 증거 서식 |

새 기능을 시작할 때 `specs/_template`을 `specs/<feature>`로 복사하고, 관련 정책·경계·계약을 확인한다. 제품 정책은 `docs/product/policies.md`, HTTP 계약은 `docs/contracts/openapi.yaml`, 기술 결정은 ADR에 기록한다.

## 의존 관계와 개발 규칙

`api → contracts + Temporal client`, `worker → contracts + Temporal SDK`, `provider adapter → 승인된 목표 구조와 Terraform/Helm 입력` 순으로 의존한다. Workflow는 Activity를 호출하지만 직접 외부 I/O를 하지 않는다. 업무 PostgreSQL 연결과 Alembic 기반만 준비했으며 실제 테이블은 아직 없다. Temporal 실행 이력은 별도 DB에 저장된다. 업무 스키마가 정해지면 모델과 검토된 Alembic revision을 추가한다.

`make check`로 형식 검사와 테스트를 실행한다. 실제 Activity를 붙일 때는 입력/출력 계약, 시간 제한, 재시도 가능 오류, 중복 실행 방지, Heartbeat, 실패 복구를 기능 명세에 적는다. 특히 Terraform Apply와 DB Cutover는 승인된 변경 불가능한 계획과 별도 정책 없이는 연결하지 않는다.

## 현재 제한

제공된 제안서의 외부 AWS 자동 스캔·온프레미스·Route 53과 기존 합의 문서의 플랫폼 관리 앱·AWS→GCP·Cloudflare 사이에는 범위 차이가 있다. 이 저장소는 기존 기본 시나리오를 문서화하고, 실제 공급자·트래픽 구현은 아직 선택하지 않았다. 로컬 Compose는 학습·개발용이며 운영 배포 설정이 아니다.
