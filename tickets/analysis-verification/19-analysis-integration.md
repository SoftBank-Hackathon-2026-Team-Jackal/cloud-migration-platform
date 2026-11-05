# Ticket 19 — 분석 Activity 통합

## 상태
IN_PROGRESS — 구현 완료(가짜 LLM·AWS 미설정 기준), 리뷰 대기, Issue #91

## 목적
`AnalysisWorkflow`의 mock Activity(02)를 03~08B·06에서 만든 실제 함수로 연결해, 소스 확보 → 스캔 → 요구사항 조립 → 사전 점검 → 매핑 → 근거 확인 → 검토 대기가 한 번에 돌게 한다. 큰 결과는 저장소에 두고 Workflow 이력에는 참조와 작은 메타데이터만 남긴다.

## 관련 요구사항
AV-001, AV-002~AV-008, AV-017, AV-020, AV-021

## 선행 작업
Blocked by: 03, 04, 05, 06, 07, 08, 08B. 분석 입력 모양(`AnalysisInput`)은 이 티켓에서 정하고, Ticket 18(공개 GitHub 소스 입력, #60)이 API에서 그 입력을 채운다(둘 다 분석 담당 범위).

## 결정 (D-02, 2026-11-05, 승철 → 민서님께 통보)
- 첫 사이클의 `*_ref` 저장소는 해시 이름의 로컬 불변 저장소다. 민서님의 `LocalArtifactStore`(API, "P0 platform artifact store: exact bytes under their SHA-256 filename, S3 later")와 같은 관례를 따른다(내용 바이트를 SHA-256 이름으로 저장, ID·종류·버전은 메타데이터). S3 전환은 `ArtifactStore` 인터페이스 뒤에서 후속으로 한다.
- 분석 결과(스캔 원본, 요구사항 바이트, 매핑 행, 검토 기록)와 Build 결과의 저장·정규화는 분석 쪽(승철)이 맡는다. 요구사항 JSON의 정확한 바이트 SHA-256이 `requirements_ref`다.

## 작업 범위
- 포함: `ArtifactStore`(`put`/`get`, 참조 `{id, version, kind, sha256, size_bytes}`, 설정 `ANALYSIS_ARTIFACT_STORE_DIR`), 분석 입력 모델(`migration_id`, 소스 저장소·요청 커밋·토큰 참조 없음 또는 참조, 대상 선택 참조 — 18과 합의한 모양), Activity별 실제 연결(`fetch_source`: `ANALYSIS_WORKSPACE_ROOT/<migration>/<commit>`, `scan_code`: Go 어댑터와 `.env` 이름, `scan_manifests`, `scan_aws_infra`·`scan_network`: 계정 설정이 없으면 "미수집"으로 표시, `build_requirements`, `precheck`, `map_usage`: 주입한 `LLMClient`(기본은 비활성), `check_evidence`), 02의 병렬 스캔·재시도·타임아웃·Heartbeat 유지, 결과 저장과 참조 전달, 검토 게이트(08B)까지 연결
- 제외: 실제 Claude 연결(ADR-004 이후), S3 저장소, Planning 인계(15), Sandbox 루프(11~14), 공개 GitHub 입력 검증과 API 변경(18)

## 수용 기준
- [x] fixture 저장소(로컬 bare repo)와 fixture 매니페스트로 `AnalysisWorkflow`가 검토 대기까지 도달하고 Query가 매핑 행·필수 여부 제안을 돌려준다(가짜 LLM).
- [x] Workflow 이력에는 참조와 작은 메타데이터만 있고, 스캔 원본·소스·`.env` 값·비밀이 없다.
- [x] 같은 입력으로 다시 실행하면 같은 `requirements_ref`(바이트 해시)가 나온다.
- [x] 클라우드 계정 설정이 없으면 인프라·네트워크 항목이 "미수집"으로 표시되고 나머지는 계속된다.
- [x] Activity를 재시도해도 작업 공간·저장소가 깨지지 않는다(같은 커밋 재사용, 같은 해시는 다시 쓰지 않음).
- [x] 분석 Activity는 한 Worker 프로세스에서 도는 것을 전제로 하고(로컬 작업 공간), 그 제약이 문서에 적힌다.
- [x] LLM 입력에는 마스킹본만 들어간다.

## 테스트 전략
- 합의된 Test Seam: Temporal 테스트 환경 + 로컬 bare repo + fixture 매니페스트 + 가짜 LLM으로 `AnalysisWorkflow` 결과·이력·Query를 확인한다. 잡는 것: 입력·참조 연결, 이력 내용, 재현성. 못 잡는 것: 실제 AWS·실제 LLM·다중 Worker 배치.
- 실행: `uv run pytest tests/analysis/test_integration.py`, `make smoke-temporal`

## 완료 시 확인할 동작
fixture 저장소를 입력하면 분석이 끝까지 돌아 검토 대기에 멈추고, CLI로 승인하면 분석 결과와 `requirements_ref`가 나온다.

## 검증 증거
- `uv run pytest tests/analysis/test_artifact_store.py tests/analysis/test_pipeline.py tests/analysis/test_integration.py`: 해시 이름 저장소 6개, 파이프라인 12개, Temporal 통합 9개 통과.
  - 저장소: SHA-256 이름 저장, 같은 내용은 같은 참조이고 다시 쓰지 않음, 파일 권한 0600과 임시 파일 정리, 읽을 때 해시 검증(변조 거절), 없는 참조·형식이 틀린 참조 거절, JSON 키 순서와 무관한 해시.
  - 파이프라인(로컬 bare repo + fixture 매니페스트): 커밋 고정과 이름만 있는 `.env`, 스캔 결과가 참조로 저장됨, 계정 설정이 없으면 AWS 스캔 `NOT_COLLECTED`·권한 거부는 필요한 권한 이름과 함께 `FAILED`로 보고하고 실행은 계속, 요구사항이 매니페스트·코드·네트워크 의도(NetworkPolicy, 코드 외부 호스트)를 합쳐 `RequirementsV2`를 통과하고 바이트 해시가 참조와 같음, 같은 입력은 같은 `requirements_ref`, 확장이 있으면 요구사항 없이 사유 보고, LLM 입력은 마스킹된 텍스트뿐, 매핑 근거 확인(존재하는 근거는 `EVIDENCE_CHECKED`, 없는 줄은 `UNVERIFIED`), 보고서에 원본 커밋·원본 DB 이미지·미수집 위험 기록, 어떤 저장물과 반환값에도 비밀이 없음.
  - 통합(`AnalysisWorkflow`): 실제 단계로 검토 대기까지 도달해 Query가 매핑 행을 돌려주고 확정·승인으로 완료, Workflow 이력에 소스 본문·비밀이 없고 크기가 작음(60KB 미만), 같은 입력은 Worker·저장소가 달라도 같은 `requirements_ref`, LLM·AWS가 없으면 끝까지 진행하며 미수집을 보고, 확장은 `ANALYSIS_BLOCKED`로 매핑 전에 멈춤, 잘못된 커밋은 재시도 없이 `SOURCE_FETCH_FAILED`, 문자열 입력은 기존 scaffold 경로 유지, 설정이 있을 때만 Worker가 실제 단계를 씀.
- 구현: `cmp_worker/artifact_store.py`, `cmp_worker/analysis_pipeline.py`, `AnalysisActivities`(`activities/analysis.py`), `AnalysisWorkflow`(문자열이면 scaffold, dict면 실제 `AnalysisInput` 경로), `workers/analysis.py`(`build_pipeline`), 계약 `AnalysisInput`. 설정 `ANALYSIS_ARTIFACT_STORE_DIR`·`ANALYSIS_RESOURCES_DIR`·`ANALYSIS_AWS_REGION`을 추가했다. `scan_network`는 실제 경로에서 별도 Activity가 아니라 `build_requirements` 안에서 요구사항의 리소스 ID와 함께 변환한다(리소스 ID가 요구사항 조립 뒤에 정해지기 때문).
- `network.build_network`가 AWS 스캔 없이도(`None`) 코드의 외부 호스트 의도만 만든다.
- 문서 변경: plan §4에 통합 경로와 입력 변화를 기록했다.

## 한계와 미검증
- 실제 Claude 연결이 없다. `map_usage`는 LLM 팩토리가 없으면 `NOT_RUN`이고 매핑 행이 없으면 검토 없이 완료한다(ADR-004 이후 연결).
- 실제 AWS 계정과 EKS 매니페스트로 돌리지 않았다. 매니페스트는 저장소 안 경로(`manifests_dir`)에서만 읽고 클러스터에서 직접 읽는 경로는 없다.
- 저장소는 로컬 디렉터리다(S3 후속). 로컬 작업 공간 때문에 분석 Activity는 한 Worker 프로세스에서 도는 것을 전제로 하며 다중 Worker 배치는 검증하지 못했다.
- 부모 `MigrationWorkflow`는 아직 `AnalysisInput`을 넘기지 않는다(문자열 scaffold). API가 저장소·커밋을 받는 입력 연결은 Ticket 18이다.
- 분석 Activity에 Heartbeat를 넣지 않았다. 긴 스캔은 Activity 타임아웃 안에서만 안전하다.
- 기존 진행 중 분석 자식 이력과의 replay는 새 경로가 별도 입력 형태(dict)일 때만 쓰이므로 문자열 입력 경로의 동작은 바뀌지 않지만, 그 replay 자체를 새로 검증하지는 않았다.

## 결정 대기
- 부모 → 자식 Workflow 입력을 `AnalysisInput`으로 바꾸는 방식의 버전 분기(기존 이력 replay 검토). Ticket 18은 그 입력을 API에서 채우는 일만 한다.
- 실제 LLM 연결(ADR-004)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 02~08B·06의 "Activity 연결은 후속" 항목을 한곳에 모으려고 신설. D-02는 해시 이름 로컬 저장소로 결정.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
