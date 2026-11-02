# Temporal 사용 가이드

이 문서는 현재 저장소의 Temporal 코드를 읽고 실행하는 방법을 설명한다. **현재 Activity는 모두 `scaffold` 예제**이며 AWS 조회, 이미지 빌드, Terraform 실행, 데이터 복사, 트래픽 전환을 하지 않는다. 완료 상태도 실제 이전 완료가 아닌 `scaffold_complete`이다.

## 1. Temporal에서 각각 맡는 일

| 개념 | 이 프로젝트의 위치 | 역할 |
| --- | --- | --- |
| Temporal Server | `infra/local/compose.yaml` | Workflow 이력·상태·Task Queue를 저장하고 작업을 Worker에 전달 |
| Client | `apps/api/src/cmp_api/temporal_client.py` | API에서 Workflow 시작, Signal 전송, Query 실행 |
| Workflow | `apps/worker/src/cmp_worker/workflows/migration.py` | 전체 단계의 순서, 승인 대기, 취소 확인, Activity 호출 |
| Activity | `apps/worker/src/cmp_worker/activities/` | 분석·빌드·인프라·데이터 이전 등 실제 외부 작업의 자리 |
| Worker | `apps/worker/src/cmp_worker/workers/` | 지정한 Task Queue를 폴링하고 Workflow 또는 Activity 실행 |
| Task Queue 이름 | `packages/contracts/src/cmp_contracts/queues.py` | API·Workflow·Worker가 동일한 Queue 이름을 사용하도록 공유 |
| Signal | Workflow의 `approve`, `request_cancel` | 실행 중인 Workflow에 승인·취소 의사 전달 |
| Query | Workflow의 `status` | 현재 단계와 승인 대기 상태 조회; Workflow 상태를 변경하지 않음 |

**Workflow는 순서를 결정하고, Activity는 외부 작업을 수행한다.** Workflow 안에서 AWS API, DB, 파일, 네트워크, 현재 시각·랜덤값 같은 비결정적 작업을 직접 사용하지 않는다. Temporal은 Workflow 이력을 재생하므로 같은 이력에서 같은 결정이 나와야 한다.

## 2. 바운더리와 Task Queue

현재 경계는 별도 저장소나 마이크로서비스가 아니라 **코드 책임과 Task Queue**로 나뉜다.

| 바운더리 | Task Queue | 실행 Worker | 현재 Activity / 책임 |
| --- | --- | --- | --- |
| 전체 흐름 | `migration` | `workers/migration.py`의 Workflow Worker | `MigrationWorkflow` 실행, 승인 대기, 단계 조정 |
| 분석 | `analysis` | `workers/analysis.py` | `analyze_project`: 소스·실행 환경 분석 자리 |
| 빌드·검증 | `build` | `workers/build.py` | `prepare_image`: AI 수정·샌드박스·이미지 준비 자리 |
| 계획·배포 | `provisioning` | `workers/provisioning.py` | `prepare_plan`, `provision_target`: 계획과 대상 준비 자리 |
| 데이터·전환 | `migration-activities` | `workers/migration.py`의 Activity Worker | `copy_data`, `shift_traffic`, `verify_target` 자리 |

`migration`과 `migration-activities`는 다르다. 앞의 Queue는 **Workflow 실행**, 뒤의 Queue는 **데이터 이전 등의 Activity 실행**에 쓴다. `MigrationWorkflow._step()`이 Activity의 `task_queue`를 지정하고, 해당 Queue를 폴링하는 Worker가 실행한다. 해당 Worker가 꺼져 있으면 그 단계는 완료되지 않고 Queue에서 기다린다.

로컬 편의를 위해 `make workers`는 다섯 Worker를 **한 프로세스**에서 함께 실행한다. `make worker QUEUE=analysis`처럼 한 경계만 실행할 수도 있다. 배포 단위와 Worker 수는 아직 정하지 않았으며, Queue 분리가 곧 독립 배포나 장애 격리를 뜻하지는 않는다.

## 3. 지금 실행되는 Workflow

```text
POST /migrations
  → analysis: analyze_project
  → build: prepare_image
  → provisioning: prepare_plan
  → awaiting_approval: approve 또는 request_cancel Signal 대기
  → provisioning: provision_target
  → migration-activities: copy_data
  → migration-activities: shift_traffic
  → migration-activities: verify_target
  → scaffold_complete
```

Workflow는 각 Activity를 `start_to_close_timeout=1분`, `maximum_attempts=2`로 호출한다. 이는 **예제 설정**이다. 실제 Terraform Apply나 데이터 복사에 그대로 적용하지 않는다. 실제 작업에는 단계별 timeout, 재시도 가능한 오류, 중복 실행 방지, Heartbeat와 취소 방식을 각각 설계해야 한다.

`request_cancel` Signal은 승인 대기 중 또는 Activity 사이에서 확인하는 **협조적 취소**다. 이미 실행 중인 Activity를 즉시 중단하지 않는다. Activity가 계속 실패하면 Temporal의 재시도 후 Workflow가 실패하며, 이 예제는 자동 복구나 보상 작업을 수행하지 않는다.

## 4. 로컬에서 사용하기

저장소 루트에서 준비한다.

```bash
cp .env.example .env
make setup
make infra-up
```

터미널 A에서 `make workers`, 터미널 B에서 `make api`를 실행한다. Temporal UI는 `http://localhost:8080`, FastAPI 문서는 `http://localhost:8000/docs`이다. 한 바운더리씩 확인할 때는 `make workers` 대신 아래 명령들을 **각각 별도 터미널**에서 실행한다.

```bash
make worker QUEUE=workflow
make worker QUEUE=analysis
make worker QUEUE=build
make worker QUEUE=provisioning
make worker QUEUE=migration
```

API로 승인 흐름을 실행한다. 첫 명령이 돌려준 `workflow_id`를 `WORKFLOW_ID`에 넣는다.

```bash
curl -X POST http://localhost:8000/migrations \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"demo"}'
WORKFLOW_ID='첫 응답의 workflow_id'
curl "http://localhost:8000/migrations/$WORKFLOW_ID"
curl -X POST "http://localhost:8000/migrations/$WORKFLOW_ID/approve"
curl "http://localhost:8000/migrations/$WORKFLOW_ID"
curl -N "http://localhost:8000/migrations/$WORKFLOW_ID/events"
```

승인은 조회 결과의 `phase`가 `awaiting_approval`일 때 보내는 것이 흐름을 이해하기 쉽다. 다른 실행에서 `/approve` 대신 `/cancel`을 호출하면 취소 흐름을 볼 수 있다. `/events`는 API가 Query를 1초마다 확인하고 **단계가 바뀔 때만** SSE를 보낸다. Temporal Event History 전체나 Activity 로그를 스트리밍하는 구현은 아니다.

API 없이 전체 경계를 한 번 검증하려면 `make infra-up` 후 `make smoke-temporal`을 실행한다. 종료 시 `make infra-down`을 사용한다. 이 명령은 컨테이너를 내리지만 DB 볼륨은 보존한다.

## 5. 새 Activity를 붙이는 순서

1. `specs/<feature>/`에 입력, 출력, 수용 기준, 실패·재시도·취소 동작을 적는다. 경계별 Pydantic 모델이 필요하면 `packages/contracts`에 추가한다.
2. 해당 `activities/*.py`에 외부 작업을 구현한다. 입력·출력은 Temporal이 직렬화할 수 있어야 한다. 비밀값 자체보다는 승인된 참조를 전달한다.
3. 대응하는 `workers/*.py`의 `activities=[...]`에 함수를 등록한다. 새 경계라면 `queues.py`에 Queue 이름을 추가하고 `run.py`, `run_all.py`에도 Worker를 연결한다.
4. `workflows/migration.py`에서 Activity를 호출하고 Queue, timeout, retry를 설정한다. Workflow에는 외부 I/O를 넣지 않는다.
5. API가 새 입력이나 상태를 보여줘야 하면 `routes/migrations.py`, 계약 모델, `docs/contracts/openapi.yaml`을 함께 갱신한다. OpenAPI 파일은 `make openapi`로 생성한다.
6. `make check`와 `make smoke-temporal`을 실행한다. 실제 클라우드 작업은 자격 증명·승인·정리 정책이 정해진 뒤 별도 통합 검증을 한다.

현재 `prepare_plan`은 실제 불변 계획 파일을 만들지 않는다. 실제 `provision_target`을 연결하기 전에는 승인 대상 계획의 ID·Digest, 저장 위치, 승인자, 변경 방지와 재시도 시 중복 Apply 방식을 먼저 확정해야 한다.
