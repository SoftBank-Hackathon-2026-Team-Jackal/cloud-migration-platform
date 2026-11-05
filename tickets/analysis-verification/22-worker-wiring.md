# Ticket 22 — Sandbox 워커 실제 조립과 실행 준비

## 상태
IN_PROGRESS — 구현 완료(단위 테스트, 이 Mac의 사전 점검 실행), 리뷰 대기, Issue #120

## 목적
배포된 워커가 실제 Sandbox 루프를 등록하게 하고, 실제 첫 사이클을 시작·점검·진행하는 방법을 갖춘다. 21번까지는 테스트가 직접 조립했을 때만 실제 경로가 돌았다.

## 관련 요구사항
AV-001, AV-015, AV-021

## 선행 작업
Blocked by: 14B, 20, 21 (모두 병합됨)

## 작업 범위
- 포함: 설정에서 `SandboxLoopActivities`를 만드는 `build_sandbox_loop`(전용 클러스터 컨텍스트 고정, 분석 저장소 공유, 모델 공장 연결)와 `create_worker` 자동 연결, 이미 만든 이미지를 Digest 고정으로 주입하는 `PrebuiltImages`(D-12 전 임시), 시작 입력 조립 `build_start_input`과 `scripts/start_migration.py`, 비용·부작용 없는 사전 점검 `cmp_worker.preflight`와 `scripts/preflight.py`, 개발 전용 로컬 저장소 허용 설정, `.env.example`, 실행 안내 `specs/analysis-verification/runbook.md`
- 제외: 실제 Build·S3(D-12, D-06), Planning 인계(15), API·화면, 만료 Sandbox 주기 정리

## 수용 기준
- [x] 설정에 클러스터 kubeconfig와 분석 저장소가 있으면 Sandbox 워커가 실제 루프 Activity를 등록하고, 없으면 기존 scaffold만 등록한다.
- [x] 루프는 전용 컨텍스트(`k3d-cmp-sandbox`)만 쓰고, 분석 워커와 같은 저장소를 읽고 쓴다. 모델은 켜고 키와 장부가 있을 때만 연결된다.
- [x] 앱 이미지는 Digest로 고정된 것만 받는다. 이미지가 없거나 태그만 있으면 시작 시 또는 `sbx_build`에서 재시도 없이 이유 코드로 실패한다.
- [x] 사전 점검이 도구·Temporal·저장소·클러스터·모델 설정·로컬 저장소 허용 여부의 빠진 것을 알려 주고, 모델 키는 출력하지 않는다.
- [x] 시작 스크립트의 입력이 부모 Workflow가 기대하는 모양이고, 설정 오타는 거절된다.
- [x] 실행 순서·설정·문제 해결이 문서로 남는다.

## 테스트 전략
- 제안 Test Seam: 설정 → 조립 결과(순수), 사전 점검(주입한 실행기), 시작 입력(순수). 잡는 것: 설정 누락·잘못된 값, 시작 모양. 못 잡는 것: 실제 환경에서의 기동(문서의 절차로 확인).
- 실행: `uv run pytest tests/sandbox/test_worker_wiring.py tests/sandbox/test_preflight.py`, `uv run python scripts/preflight.py`

## 완료 시 확인할 동작
`.env`를 채우고 `make workers`로 워커를 띄우면 사전 점검이 통과하고, 시작 스크립트로 실제 첫 사이클을 시작할 수 있다.

## 구현 요약
- `workers/sandbox.py`: `build_sandbox_loop(config)`가 `SANDBOX_KUBECONFIG`와 `ANALYSIS_ARTIFACT_STORE_DIR`이 모두 있을 때만 만들고, 없으면 `None`(scaffold만). kubeconfig 파일이 없으면 시작 시 `ValueError`. 차트는 저장소의 `infra/helm`.
- `prebuilt_builder.py`: `PrebuiltImages`(`SANDBOX_PREBUILT_IMAGES` JSON). 코드: `BUILD_NOT_CONFIGURED`, `PREBUILT_IMAGE_MISSING`, `PREBUILT_IMAGE_NOT_DIGEST_PINNED`, `PREBUILT_IMAGES_INVALID`. `sbx_build`는 이를 재시도 없는 `ApplicationError`로 바꾼다.
- `config.py`: `sandbox_*` 설정과 `analysis_allow_local_repositories`(개발 전용, 기본 꺼짐). 분석 워커가 이 값을 파이프라인에 넘긴다.
- `preflight.py`, `scripts/preflight.py`, `start_input.py`, `scripts/start_migration.py`.

## 검증 증거
- 단위: `tests/sandbox/test_worker_wiring.py`(10), `tests/sandbox/test_preflight.py`(10).
- 이 Mac에서 `scripts/preflight.py` 실행: 도구는 모두 있고(helm 3.22), Temporal이 꺼져 있고 `ANALYSIS_*`·`SANDBOX_*` 설정이 비어 있다고 정확히 보고한다. 실제 기동은 runbook의 순서대로 준비한 뒤 확인한다.

## 한계와 미검증
- 실제 워커 기동과 시작 스크립트로 시작하는 전체 흐름은 이 Mac에서 아직 돌리지 않았다(Temporal·설정 준비 전).
- 실제 Build, S3, Planning 인계, API·화면, 주기 정리는 이 티켓에 없다.

## 관련 문서
- [실행 안내](../../specs/analysis-verification/runbook.md) · [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [ADR-004](../../docs/decisions/ADR-004.md)

## 설계 변경 기록
- 2026-11-05: 신설. 배포된 워커에서 실제 경로가 돌지 않는 문제와 실행 준비를 한 티켓으로 묶음.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
