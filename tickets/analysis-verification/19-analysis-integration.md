# Ticket 19 — 분석 Activity 통합

## 상태
TODO — 사용자 결정으로 착수(D-02 결정 반영). Issue 번호는 발행 후 기입

## 목적
`AnalysisWorkflow`의 mock Activity(02)를 03~08B·06에서 만든 실제 함수로 연결해, 소스 확보 → 스캔 → 요구사항 조립 → 사전 점검 → 매핑 → 근거 확인 → 검토 대기가 한 번에 돌게 한다. 큰 결과는 저장소에 두고 Workflow 이력에는 참조와 작은 메타데이터만 남긴다.

## 관련 요구사항
AV-001, AV-002~AV-008, AV-017, AV-020, AV-021

## 선행 작업
Blocked by: 03, 04, 05, 06, 07, 08, 08B. 입력 경계는 Ticket 18(공개 GitHub 소스 입력, #60)과 맞춘다.

## 결정 (D-02, 2026-11-05, 승철 → 민서님께 통보)
- 첫 사이클의 `*_ref` 저장소는 해시 이름의 로컬 불변 저장소다. 민서님의 `LocalArtifactStore`(API, "P0 platform artifact store: exact bytes under their SHA-256 filename, S3 later")와 같은 관례를 따른다(내용 바이트를 SHA-256 이름으로 저장, ID·종류·버전은 메타데이터). S3 전환은 `ArtifactStore` 인터페이스 뒤에서 후속으로 한다.
- 분석 결과(스캔 원본, 요구사항 바이트, 매핑 행, 검토 기록)와 Build 결과의 저장·정규화는 분석 쪽(승철)이 맡는다. 요구사항 JSON의 정확한 바이트 SHA-256이 `requirements_ref`다.

## 작업 범위
- 포함: `ArtifactStore`(`put`/`get`, 참조 `{id, version, kind, sha256, size_bytes}`, 설정 `ANALYSIS_ARTIFACT_STORE_DIR`), 분석 입력 모델(`migration_id`, 소스 저장소·요청 커밋·토큰 참조 없음 또는 참조, 대상 선택 참조 — 18과 합의한 모양), Activity별 실제 연결(`fetch_source`: `ANALYSIS_WORKSPACE_ROOT/<migration>/<commit>`, `scan_code`: Go 어댑터와 `.env` 이름, `scan_manifests`, `scan_aws_infra`·`scan_network`: 계정 설정이 없으면 "미수집"으로 표시, `build_requirements`, `precheck`, `map_usage`: 주입한 `LLMClient`(기본은 비활성), `check_evidence`), 02의 병렬 스캔·재시도·타임아웃·Heartbeat 유지, 결과 저장과 참조 전달, 검토 게이트(08B)까지 연결
- 제외: 실제 Claude 연결(ADR-004 이후), S3 저장소, Planning 인계(15), Sandbox 루프(11~14), 공개 GitHub 입력 검증과 API 변경(18)

## 수용 기준
- [ ] fixture 저장소(로컬 bare repo)와 fixture 매니페스트로 `AnalysisWorkflow`가 검토 대기까지 도달하고 Query가 매핑 행·필수 여부 제안을 돌려준다(가짜 LLM).
- [ ] Workflow 이력에는 참조와 작은 메타데이터만 있고, 스캔 원본·소스·`.env` 값·비밀이 없다.
- [ ] 같은 입력으로 다시 실행하면 같은 `requirements_ref`(바이트 해시)가 나온다.
- [ ] 클라우드 계정 설정이 없으면 인프라·네트워크 항목이 "미수집"으로 표시되고 나머지는 계속된다.
- [ ] Activity를 재시도해도 작업 공간·저장소가 깨지지 않는다(같은 커밋 재사용, 같은 해시는 다시 쓰지 않음).
- [ ] 분석 Activity는 한 Worker 프로세스에서 도는 것을 전제로 하고(로컬 작업 공간), 그 제약이 문서에 적힌다.
- [ ] LLM 입력에는 마스킹본만 들어간다.

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경 + 로컬 bare repo + fixture 매니페스트 + 가짜 LLM으로 `AnalysisWorkflow` 결과·이력·Query를 확인한다. 잡는 것: 입력·참조 연결, 이력 내용, 재현성. 못 잡는 것: 실제 AWS·실제 LLM·다중 Worker 배치.
- 실행: `uv run pytest tests/analysis/test_integration.py`, `make smoke-temporal`

## 완료 시 확인할 동작
fixture 저장소를 입력하면 분석이 끝까지 돌아 검토 대기에 멈추고, CLI로 승인하면 분석 결과와 `requirements_ref`가 나온다.

## 결정 대기
- Ticket 18의 입력 계약(공개 소스 표현, 부모 → 자식 Workflow 입력 변경 방식)
- 실제 LLM 연결(ADR-004)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 02~08B·06의 "Activity 연결은 후속" 항목을 한곳에 모으려고 신설. D-02는 해시 이름 로컬 저장소로 결정.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
