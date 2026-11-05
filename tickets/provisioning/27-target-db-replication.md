# Ticket 27 — 대상 DB 논리 복제 준비와 복제 가능 판정 보정

## 상태
DONE (로컬 범위) — [Issue #151](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/151). 2026-11-06 점검 결과에 따른 사용자 지시로 착수. 자체 리뷰 완료, 조정 세션 리뷰·push 대기. 새 Agent 릴리스 필요(태그 미생성).

## 목적
대상 PostgreSQL이 논리 복제의 publisher가 될 수 있게 띄운다(전환 후 롤백용 역방향 복제). 결과의 `replication_capable`이 역할 권한뿐 아니라 실제 서버 설정까지 확인한 값이 되게 한다. 지금은 PG16 기본 `wal_level=replica`로 떠서 역방향 복제가 불가능한데도, 기본 계정이 superuser라 판정은 항상 `true`다. ON_PREM은 판정하지 않는다.

## 관련 요구사항
PROV-005, PROV-011, PROV-018, PROV-020

## 선행 작업
Blocked by: 02, 04B, 09, 11, 12, 13 (DONE). 원본 DB 점검은 Data Migration 범위(분석 명세 기준).

## 작업 범위
- 포함
  - DB Chart Values 계약 `database.parameters`(허용 목록)와 Chart의 `postgres -c k=v` 실행 인자, `values.schema.json` 재생성
  - v2 공통 렌더러의 기본값(논리 복제 가능) — Plan 게시(ON_PREM·AWS/GCP)와 Sandbox가 같은 렌더러를 쓴다
  - 공통 판정 함수와 SQL(`cmp_providers.db_replication`), AWS/GCP 배포 Activity(09·11)의 판정·원인 코드
  - ON_PREM Agent의 같은 판정과 결과 보고(선택 필드), API의 수용·결과 인계 반영
  - Preview의 DB 항목 표시, SDD·Chart 문서·OpenAPI 갱신
- 제외: 원본 DB 점검, publication·slot 생성(Data Migration), 사용자 입력으로 파라미터 바꾸기(입력 계약 v2 변경 없음), v1 공통 렌더러(`shared_deployment.py`)

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| U1 Values 필드 | `database.parameters`: `wal_level`(`replica`·`logical`), `max_replication_slots`·`max_wal_senders`(0~64 정수), `app`(키 `^[a-z][a-z0-9_]{0,30}$`, 값 `^[A-Za-z0-9._-]{1,63}$`, 최대 8개 → `app.<키>`). 정해진 필드 외는 계약·스키마가 거절한다 | 임의 `-c` 플래그를 열지 않는다. `app.*`는 데모 앱의 `app.node_name` 같은 표시용 사용자 설정이다 |
| U2 이전 Values 호환 | 필드가 없으면 직렬화에서 빠진다(`exclude_if`). 이미 승인된 Values의 바이트·Digest가 그대로이고, Chart도 인자를 넣지 않는다 | 19와 같은 방식 |
| U3 기본값 | v2 렌더러가 `wal_level=logical`, 슬롯 10, 송신자 10, `app.node_name=<provider 소문자>-<DB release>`를 넣는다 | 원본(`source.yaml`)은 `logical`·4·4. 10·10은 PG16 기본값이라 원본 이상이고 메모리 영향이 없다. 바뀌는 것은 `wal_level`뿐이며 재시작이 필요한 설정이라 배포 시점에 정한다. 노드 이름은 이전 뒤 새 행이 어느 DB에서 쓰였는지 데모 앱에 보이게 한다 |
| U4 Chart 인자 | `parameters`가 있을 때만 `args: [-c, wal_level=…, -c, max_replication_slots=…, -c, max_wal_senders=…, -c, app.<키>=…]`(키 정렬). 공식 이미지 entrypoint가 `postgres`를 앞에 붙인다 | 컨테이너 args 배열이라 셸 해석이 없다 |
| U5 판정 | 한 번의 psql 질의로 `버전|역할|wal_level|슬롯|송신자`를 읽고 공통 함수가 판정한다. 역할(`rolreplication or rolsuper`)·`wal_level='logical'`·슬롯>0·송신자>0을 모두 만족해야 `true`. 원인 코드는 처음 실패한 항목 하나: `REPLICATION_ROLE_MISSING`, `WAL_LEVEL_NOT_LOGICAL`, `REPLICATION_SLOTS_UNAVAILABLE`, `WAL_SENDERS_UNAVAILABLE` | 권한만 보면 항상 true다 |
| U6 판정 실패 처리 | 배포는 실패시키지 않는다. `READY` 유지, `replication_capable=false`, 원인 코드를 결과 `warnings`에 남긴다(결과 계약 필드 추가 없음) | 워크로드 준비와 복제 준비는 다른 판단이고, 이전 Values로 재배포한 기존 경로가 실패로 바뀌지 않게 한다(수용 기준 "회귀 없음"). Data Migration은 `replication_capable`로 판단한다 |
| U7 ON_PREM | Agent가 READY 확인 뒤 같은 SQL을 DB 컨테이너에서 실행해 결과 보고에 선택 필드 `replication {capable, code}`를 붙인다. API는 ROLLOUT 작업에만 이 필드를 받고 결과 인계 refs로 옮긴다. 판정 명령이 실패하면 필드를 빼서 `REPLICATION_NOT_VERIFIED`가 남는다 | 이전 Agent는 필드를 보내지 않아 지금처럼 `null`+경고로 남는다. 새 Agent 릴리스가 필요하다(태그는 이 티켓에서 만들지 않음) |
| U8 Preview | DB 항목 크기 문구 끝에 `논리 복제 설정 wal_level=logical · 슬롯 10 · 송신자 10`을 붙인다. 필드가 없는 이전 Values는 문구가 그대로다 | 승인자가 재시작이 필요한 DB 설정을 본다 |

## 수용 기준
- [x] 세 대상 DB가 `wal_level=logical`로 뜨고(새 Plan), 판정이 설정까지 확인한다.
- [x] 설정이 맞지 않으면 `replication_capable=false`와 원인 코드가 남는다.
- [x] 이전 Values의 바이트·Digest와 렌더 결과, 09·11·12 경로 회귀 없음.

## Test Seam
- Chart: 실제 `helm template` 결과의 args, 스키마의 알 수 없는 파라미터·잘못된 값 거절, 이전 Fixture 렌더 불변.
- 계약: 필드 없는 Values의 바이트 불변, v2 렌더러 기본값.
- 판정 함수: 역할 OK + `wal_level=replica` → `false`+`WAL_LEVEL_NOT_LOGICAL` 등.
- AWS/GCP: 09·11의 가짜 ClusterSession. ON_PREM: 12의 가짜 클러스터·Agent 보고·API 결과 인계.
- 놓치는 것: 실제 클러스터에서 publication·slot 생성(가능하면 k3d로 확인).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰; 조정 세션 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## 검증 기록 (2026-11-06)

| 명령·환경 | 결과 |
| --- | --- |
| `make check` (ruff + pytest, Helm v4) | 1781 passed, 8 skipped |
| `uv run pytest tests/test_db_replication.py` | 필드 없는 Values의 바이트 불변, 허용 목록 밖·잘못된 값 거절(계약·Helm 스키마), 실제 `helm template`의 `args`, 필드 없으면 `args` 없음, 판정 함수 6+3개 경우, Preview 문구, v2 렌더러 기본값 |
| `tests/test_aws_workloads.py` (09·11 공통 Activity) | 역할 OK + `wal_level=replica` → READY 유지, `db_replication_capable=false`, `db_replication_code=WAL_LEVEL_NOT_LOGICAL`; 역할 없음 → `REPLICATION_ROLE_MISSING`. GCP 가짜 ClusterSession은 새 형식으로 회귀 통과 |
| `tests/test_onprem_agent.py`·`test_onprem_rollout.py` (12) | Agent가 READY 뒤 판정 결과를 보고, 판정 명령 실패면 필드 없음, 실패 롤아웃은 미보고. API는 READY 보고에서만 수용하고 인계 refs로 옮김, 이전 Agent(필드 없음)는 refs 없음 |
| `tests/test_provisioning_result.py` | ON_PREM 결과: 실측 true면 경고 없음, false면 원인 코드 경고, 미보고면 `REPLICATION_NOT_VERIFIED` |
| `tests/test_plan_publish.py` | 게시된 ON_PREM Plan의 DB Values에 `parameters`, Preview DB 항목 끝에 설정 문구 |
| 격리 k3d(`cmp-t27`, 전용 kubeconfig, 기본 kubeconfig 미변경, 종료 후 삭제) | 실제 Chart+기본 파라미터로 PG 16.15 배포 → 판정 `160015\|true\|logical\|10\|10` → `(True, None)`. `CREATE PUBLICATION`·`pg_create_logical_replication_slot(pgoutput)` 성공, `app.node_name=on-prem-t27-db`. 같은 PVC로 이전 Values(필드 없음) 재배포 → `replica` → `(False, WAL_LEVEL_NOT_LOGICAL)` |
| `make openapi` | Agent 결과 보고 `JobReport`에 선택 필드 `replication`(`RolloutReplication {capable, code}`) 추가. 검토 완료 |
| `alembic check` (SQLite, head까지 upgrade) | 변경 없음 — DB 마이그레이션 없음 |

구현 메모: 실패 테스트를 먼저 작성했다. `DB_PROBE`는 `cmp_providers.db_replication`으로 옮기고 Worker CLI가 다시 내보낸다(`aws_workloads_cli` import 유지). `RolloutRunner.rollout()`의 반환 형태는 그대로 두고 `runner.replication`에 판정을 담는다.

### 미검증·제한 사항
- 새 Agent 릴리스가 필요하다(15 릴리스 경로, 이 티켓에서 태그 미생성). 이전 Agent는 필드를 보내지 않아 지금처럼 `null`+`REPLICATION_NOT_VERIFIED`다. **배포 순서: 플랫폼(API) 먼저, Agent 나중.** 이전 API는 `replication` 필드가 든 보고를 422로 거절한다.
- 이미 승인된 Plan(필드 없음)으로 재배포한 대상은 `replica`로 떠서 `replication_capable=false`·`WAL_LEVEL_NOT_LOGICAL`이 된다. 논리 복제가 필요하면 Plan을 다시 게시·승인한다.
- 실제 EKS/GKE와 실제 온프레미스 Agent 경로는 실행하지 않았다(k3d에서 Chart·SQL·판정만 확인). 역방향 복제 구독 자체는 Data Migration 범위다.
- `wal_level` 변경은 PostgreSQL 재시작이 필요하다. 기존 대상에 새 Plan을 적용하면 StatefulSet Pod가 다시 시작된다.
- v1 공통 렌더러(`shared_deployment.py`)는 기본값을 넣지 않는다(v2가 Plan 게시·Sandbox 경로). `spec.md` 요구사항은 바뀌지 않았다(PROV-011 "복제 역할·설정 검증"을 구현).
