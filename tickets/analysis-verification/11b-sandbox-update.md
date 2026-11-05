# Ticket 11B — Sandbox 제자리 갱신과 테스트 데이터 초기화

## 상태
IN_PROGRESS — 구현 완료(실제 k3d `cmp-sandbox`에서 검증), 리뷰 대기, Issue #95

## 목적
시도마다 Sandbox를 새로 만들지 않고 바뀐 부분만 갱신하며, 테스트 데이터를 처음 상태로 되돌린다.

## 관련 요구사항
AV-011, AV-015

## 선행 작업
Blocked by: 11

## 작업 범위
- 포함: 갱신 규칙(이미지만 → 앱 Release 갱신, 설정·바인딩 → 재렌더, env Secret 내용 → Pod 재시작 보장, 바꿀 수 없는 필드·DB 메이저 버전 → 해당 Release 재생성), `reset_test_data`, 시도별 Digest·렌더 결과 기록
- 제외: 실패 분류·수정(14)

## 수용 기준
- [x] 이미지 Digest만 바꾸면 앱 Release만 갱신되고 DB는 그대로다.
- [x] env Secret 내용만 바꿔도 앱 Pod가 새 값으로 다시 뜬다.
- [x] 시도마다 테스트 데이터가 처음 상태로 돌아간다.
- [x] 시도별 Digest·Values SHA-256이 기록된다.

## 테스트 전략
- 제안 Test Seam: 격리 k3d에서 갱신 전후 클러스터 상태. 잡는 것: 갱신 범위·재시작·초기화. 못 잡는 것: 운영 데이터 규모의 마이그레이션 시간.
- 실행: `uv run pytest tests/integration/sandbox -m k3d -k update`

## 완료 시 확인할 동작
같은 Sandbox에서 이미지 교체와 설정 변경을 연속으로 적용해도 테스트가 같은 시작 상태에서 돈다.

## 구현 요약
- `plan_update(previous, current)`: 리소스별 UNCHANGED / UPGRADED / RESTARTED / RECREATED. DB 메이저 버전·저장소 크기·StorageClass·DB 이름·Release 이름 변경은 RECREATED, 같은 메이저의 이미지·자원 변경은 UPGRADED, 앱은 어떤 변경이든 UPGRADED, env Secret만 바뀌면 RESTARTED. 리소스 집합이 바뀌면 `RESOURCE_SET_CHANGED`.
- env Secret 변경 감지: 생성 시 `cmp.sandbox/content-sha256` 어노테이션(run_id를 섞은 해시)을 달고, `prepare_sandbox(update_env=True)`가 클러스터의 해시와 다를 때만 Secret을 교체한다. 값은 stdin으로만 전달한다.
- `update_sandbox`: UNCHANGED는 Helm을 부르지 않는다. RECREATED DB는 `helm uninstall --wait` → PVC 삭제 → 설치 → 앱 재시작, 기준선은 `LOST`.
- `snapshot_baseline` / `reset_test_data`: DB Pod 안에서 `pg_dump -Fc`(PGDATA 볼륨 위 `cmp-baseline.dump`)와 `DROP SCHEMA public CASCADE` + `pg_restore`. 비밀번호는 Pod 환경변수에서만 읽고 명령 인자에 싣지 않는다. 기준선이 없으면 `BASELINE_MISSING`. 초기화 뒤 앱을 재시작한다.
- 시도 기록 `AttemptRecord`: 리소스별 `image_digest`, `values_sha256`(렌더한 Values의 정규화 JSON), 수행한 동작. `apply_sandbox`도 같은 `resources`를 돌려준다.

## 결정 (이전 "결정 대기")
- 앱 차트에 Secret 해시 어노테이션이 없으므로 `kubectl rollout restart` + `rollout status`로 재시작을 보장한다(차트 변경 없음).

## 검증 증거
- 단위: `uv run pytest tests/sandbox` 91 passed(Helm 필요 테스트 포함).
- 실제 k3d: `CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d -k update` 통과(약 52초). 이미지만 교체 시 DB Pod UID·Helm Release가 그대로, env 값 변경 시 앱 Pod만 교체, 기준선 뒤에 만든 테이블이 `reset_test_data` 후 사라짐.
- 전체 `uv run pytest` 952 passed, 4 skipped.

## 한계와 미검증
- RECREATED 경로(uninstall → PVC 삭제 → 재설치)는 명령 순서를 단위 테스트로만 검증했고 실제 클러스터 실행은 하지 않았다.
- 기준선은 DB 볼륨 안에 둔다. 볼륨이 지워지는 RECREATED 후에는 `LOST`이며 호출부(14)가 다시 `snapshot_baseline`을 해야 한다.
- Workflow 연결과 시도 번호 부여는 14의 책임이다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백으로 기존 11에서 분리.
- 2026-11-05: 구현. 앱 차트 변경 없이 rollout restart로 재시작을 보장하기로 결정.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
