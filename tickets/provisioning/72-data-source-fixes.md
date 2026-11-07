# Ticket 72 — 실제 원본 데이터 복제를 막는 5가지 수정 (ADR-007 단계 6, 데이터 범위 대행)

## 상태
DONE (PR #300 병합, 2026-11-07 데이터 담당 확인. 실제 원본 복제·이미지 push는 승인 뒤) — [Issue #298](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/298). 2026-11-07 조율 세션 지시로 착수. 데이터 영역 코드이며 본선 일정 때문에 Provisioning(김민서)이 대행한다. 복제 단계 순서는 바꾸지 않는다.

## 배경
결선 데모는 온프레미스(맥 k3d) 대상이고 데이터 이전은 실제 PostgreSQL 논리 복제로 한다(ADR-007 2안). 실제 원본 `cmp-demo-source`/`chat-source`로 돌리면 코드상 다섯 곳에서 막힌다: worker가 데이터 전용 AWS 프로필·kubeconfig 디렉터리를 받지 못하고, prepare가 원본에 만든 marker 표 때문에 스키마 비교가 `TARGET_SCHEMA_PARTIAL`로 멈추고, cutover 이미지를 만드는 정해진 경로가 없고, 복제가 실패하면 원본 슬롯이 남아 WAL을 붙잡고, 전환 검증 체크섬이 세션 TimeZone에 따라 달라진다.

## 관련 요구사항
ADR-007(단계 6), specs/data-migration DM-004·DM-010, data-migration 티켓 02·04·05·08·12, data-migration tasks 13.

## 작업 범위
- 포함: 아래 결정 D1~D5
- 제외: 게이트웨이 터널 코드(`cmp_data.tunnel`, `data_tunnel` Activity — 67), Agent·중계 서버·웹, 복제 단계 순서, 이미지 push·서버 배포·AWS 변경, 실패 시 터널 정리

## 착수 결정 (2026-11-07, Issue #298)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | worker 자격 | compose worker에 `DATA_MIGRATION_AWS_PROFILE`·`DATA_MIGRATION_KUBECONFIG_DIR`을 빈 기본값으로, 호스트 `/opt/cmp/secrets/data-migration` → `/run/cmp/data-migration:ro`. `ensure-secrets.sh`가 디렉터리를 만들고 `runtime-check.sh`는 이름과 kubeconfig 파일 개수만 출력 | 빈 값은 지금과 같다(EKS 실패, `DbAccessNotConfigured`). 값은 서버 `.env`에서 켠다. `DATA_MIGRATION_GCLOUD_CONFIGURATION`(GKE 대상)은 이번 범위 밖 |
| D2 | 내부 표 | 내부 표 목록은 `cmp_data.sql.INTERNAL_TABLES` 한 곳(`cutover_events`가 짧은 이름을 더해 쓴다). `copy_schema`는 비교에서 빼고, pg_dump는 `--exclude-table`로 빼고, 끝에 구독자에 `ENSURE_MARKER_SQL` | `for all tables` 발행이 marker 행을 보내므로 구독자에 표가 필요하다. dump에서 빼면 구독자에 marker만 있어도 restore가 깨지지 않는다 |
| D3 | cutover 이미지 | `infra/data-migration/cutover/build-image.sh <REPOSITORY>`: `docker buildx build --platform linux/amd64 --load`, 태그 `cmp-cutover-<그 디렉터리 마지막 커밋 12자리>`, 커밋 안 된 변경이 있으면 `CUTOVER_SOURCE_DIRTY`. push는 하지 않고 명령과 `image=` 값만 출력 | push는 승인 대상. GitHub Actions는 ECR 자격이 필요해 두지 않는다. 같은 커밋 = 같은 태그 |
| D4 | 실패 시 정리 | `dm_subscribe`를 시작한 뒤 복제가 실패(Activity 오류, `SYNC_TIMEOUT`)하면 `dm_cleanup`(5회)을 시도한다. 정리 실패 코드는 결과 `cleanup_code`에만, `failure_code`는 원래 코드. `workflow.patched("data-failure-cleanup-v1")` | 기존 실행 이력 재생 호환. 구독 전 실패는 지울 것이 없다. 결과 키 추가는 부모가 읽지 않아 호환 |
| D5 | 체크섬 | 실행기가 검증용 접속(`old_v`·`new_v`)을 연 직후, PAUSE 전에 `SET TIME ZONE 'UTC'`·`SET DateStyle = 'ISO, YMD'` | 스냅샷(PAUSE 안)에서 하면 쓰기 중단 시간에 왕복이 늘어난다. DateStyle도 date 글자를 바꾼다 |

## 공개 인터페이스

```python
# cmp_data.sql
INTERNAL_TABLES: frozenset[str]           # {"public.cmp_switchover_marker"}
parse_user_tables(raw: str) -> set[str]   # USER_TABLES_SQL 결과 - INTERNAL_TABLES
# cmp_worker.activities.data_migration
DbPod.dump_schema(exclude: frozenset[str] = frozenset()) -> bytes
# DataMigrationWorkflow 결과
{"status", "active", "switches", "failure_code", "cleanup_code"}
```

## 테스트 Seam
- `tests/test_demo_runtime.py`: compose 환경·마운트, 예시 파일, ensure-secrets 디렉터리, runtime-check 이름
- `tests/data/test_data_activities.py`: 가짜 DbPod로 marker 제외 비교·dump 제외·구독자 marker 보장, pg_dump 명령의 `--exclude-table`
- `tests/data/test_data_cutover_image.py`: 가짜 docker·git으로 amd64·태그·push 없음·dirty 거절
- `tests/data/test_data_workflow.py`: 실패·정리 실패·`SYNC_TIMEOUT`·구독 전 실패
- `tests/data/test_data_cutover_events.py`: 실행기 `pin_text_format`과 PAUSE 전 호출 위치

## 검증
- `make check` 통과(로컬), 새 테스트는 구현 전 실패(RED 12건) 확인
- 실제 원본 복제·이미지 빌드/push·서버 반영은 하지 않았다(승인 대상)

## 문서
`infra/demo/README.md`·`runtime.env.example`, `infra/data-migration/crosscloud-lab/README.md`(이미지 빌드), `specs/data-migration/tasks.md` 13, `specs/provisioning/tasks.md`. API 계약 변경 없음(`make openapi` 불필요).
