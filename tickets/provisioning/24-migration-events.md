# Ticket 24 — 이전 이벤트 저장소·SSE·운영 로그 공통 모듈

## 상태
IN_PROGRESS (구현·로컬 검증 완료, 리뷰·push 대기) — [Issue #122](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/122). 2026-11-05 사용자 지시로 착수(별도 브랜치 커밋, push·PR은 사용자 확인 후).

## 목적
웹 데모는 목업 이벤트로 움직인다. 백엔드 SSE는 1초마다 부모 Workflow 상태만 조회해 `event: status`를 보내고, `seq`가 없고 `failed`에서 닫히지 않는다. 각 파트 담당자가 자기 Activity에서 웹이 기대하는 이벤트를 한 줄로 발행할 수 있게 공통 저장소·발행 API·SSE를 만들고, Provisioning 몫은 직접 연결한다.

## 관련 요구사항
PROV-014, PROV-015, PROV-018, PROV-021. 명세: [migration-events.md](../../specs/provisioning/migration-events.md). 아키텍처: [temporal-flow §12](../../docs/architecture/temporal-flow.md).

## 선행 작업
Blocked by: 07, 13, 18 (DONE). 병행: 22(PR #117, `plan_publish.py`·`workflows/migration.py` 수정), 23(로컬 브랜치, Alembic `20261105_05`). 이 티켓은 `plan_publish.py`에 발행 한 줄만 더하고 Workflow 코드는 바꾸지 않는다.

## 작업 범위
- 포함
  - `cmp_contracts.events`: 봉투·33개 타입별 `data` 모델, 이름 변환 `to_web`, 웹 스키마 형식 내보내기(`scripts/export_event_schema.py`)
  - Alembic `migration_events`·`migration_logs`, `cmp_api.migration_events`(append·list·emitter)
  - `cmp_worker.events`: `emit_event`·`emit_event_async`·`log_event`·`configure_emitter`, `emit_migration_event` Activity
  - SSE 개편과 JSON 읽기(`/migrations/{id}/events`, `/migrations/{id}/logs`)
  - Provisioning 발행: 승인·재전달 `approval.decision`, Plan 게시 `plan.ready`, Terraform `-json` → `provision.resource`, 워크로드 Helm·검사 → `provision.resource`·`provision.check`
  - 담당자 안내서 `docs/engineering/events-guide.md`와 전달 메시지 초안
  - PoC 브리지 어댑터(`cmp_worker.poc_bridge`): `/api/metrics`·readiness·switchover 결과 → `data.*`·`cutover.*`
- 제외: 분석·샌드박스·데이터·트래픽 도메인 로직 수정, 웹 저장소 수정, 실제 데이터 이전·트래픽 전환 구현

## 수용 기준
- [x] 내보낸 JSON Schema가 웹 스키마 사본과 정규형 비교 테스트를 통과한다.
- [x] `seq` 단조 증가·멱등·재접속(`Last-Event-ID`)·스트림 종료(done/failed/cancelled)가 테스트로 확인된다.
- [x] 승인·계획·구축 이벤트가 실제 Activity·API 경로에서 발행된다(가짜 Terraform·클러스터 기준).
- [x] 이벤트·로그에 비밀값 원문이 없다(마스킹 테스트).
- [x] 안내서가 파트별 발행 위치·값·예시와 "아직 없음" 목록을 담는다.
- [x] `make check`, `make openapi` 검토, `alembic check`(SQLite) 통과. PostgreSQL `alembic check`·`make smoke-temporal`은 로컬 인프라(Docker)가 꺼져 있어 미실행.

## Test Seam
명세의 "테스트 Seam" 절. 실제 클라우드·Temporal 서버 없이 SQLite·`ActivityEnvironment`·FastAPI `TestClient`로 확인한다.

## 결과
- 계약: `cmp_contracts.events`(33개 타입 data 모델), 웹 스키마 사본 `docs/contracts/events.schema.json`(웹 `Minseo` `971ce73`), `scripts/export_event_schema.py`, 정규형 비교 `tests/test_event_contract.py`.
- 저장·발행: Alembic `20261105_06`(down `20261105_04`, 23의 `_05`와 합칠 때 재연결), `cmp_api.migration_events`, `cmp_worker.events`. Worker 시작 시 `DbEventEmitter(SessionLocal)`.
- SSE: `cmp_api.event_stream` + `GET /migrations/{id}/events`(SSE·JSON), `GET /migrations/{id}/logs`. `failed` 계열 종료 phase에서 닫히지 않던 문제 수정.
- Provisioning: `approval.decision`(승인 API), `plan.ready`(`PlanReadyEvents` 래퍼 — PR #117이 바꾸는 줄을 피함), Terraform `apply -json` → `provision.resource`(`cmp_providers.terraform_events`), 워크로드 Helm·LB → `provision.resource`, `pods_ready`·`db_connect`·`app_health` → `provision.check`, 실행 결과 운영 로그.
- PoC 브리지: `cmp_worker.poc_bridge`(패널 `/api/state`·switchover 결과·실패 stderr).
- 안내서: `docs/engineering/events-guide.md`(파트별 발행표, PoC 브리지, "아직 없음").
- 결정 필요: `Evidence.kind` ↔ 웹 `via` 대응(승철님), 롤백 기준 #61 vs #63(상헌님). 웹 쪽 후속(민서): `event: status` 블록 건너뛰기 또는 `?legacy=false`, PoC fast 순서(3·4·6 동시) 표시.
- 미검증: PostgreSQL 대상 Alembic·동시성, Temporal 서버 스모크, 실제 Terraform `-json` 스트리밍(가짜 runner로만 확인).
