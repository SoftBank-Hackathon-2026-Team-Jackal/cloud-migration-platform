# Ticket 03 — 소스 확보·운영 커밋 고정·마스킹

## 상태
DONE — main에 반영됨(PR 없이 직접 push, CI 통과 확인), Issue #48

## 목적
분석할 소스를 격리된 작업 공간에 고정된 커밋으로 확보하고, 빌드용 원본과 LLM용 마스킹본을 분리한다.

## 관련 요구사항
AV-002, AV-020

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: 토큰 기반 클론(GitHub App은 후속), 실행 이미지 OCI 라벨에서 커밋 찾기(인터페이스 + stub), 작업 공간 재사용, 마스킹 함수, `.env` 이름만 파싱
- 제외: GitHub App 연동, Draft PR(14), `.env` 값 처리(11의 Secret 생성)

## 수용 기준
- [x] 커밋을 못 찾으면 사용자 지정 커밋을 쓰고 `commit_verified=false`를 남긴다.
- [x] 같은 커밋 재요청은 다시 클론하지 않는다.
- [x] 마스킹본에 알려진 키 패턴·고엔트로피 문자열·`.env` 값이 남지 않는다.
- [x] 분석 단계의 `.env` 파싱 결과에 값이 없고 이름만 있다.
- [x] 작업 공간 경로가 Migration·커밋별로 분리되고 서비스 프로세스 디렉터리 밖이다.

## 테스트 전략
- 합의된 Test Seam: `fetch_source`의 공개 함수 결과와 마스킹 함수 출력. 잡는 것: 커밋 고정·비밀 유입. 못 잡는 것: 실제 GitHub 권한·대용량 저장소 시간.
- 실행: fixture 저장소(로컬 bare repo)로 `uv run pytest tests/analysis/test_source.py`

## 완료 시 확인할 동작
fixture 저장소를 넣으면 커밋 고정된 원본·마스킹본과 `.env` 이름 목록이 나온다.

## 검증 증거
- `uv run pytest tests/analysis/test_source.py`: 27 통과. 로컬 bare repo로 요청 커밋 고정·운영 커밋 우선(확인됨)·존재하지 않는 운영 커밋의 폴백, 원격을 지운 뒤 같은 커밋 재요청(재클론 없음), Migration·커밋별 경로와 0700 권한, 프로세스 디렉터리 안 루트 거절, 경로 이탈 Migration ID 거절, https 외 저장소 거절, 실패 메시지·작업 공간에 토큰 없음, `.env`는 이름만 반환, 마스킹본에 가짜 비밀 없음과 원본 유지, 알려진 비밀 형태 마스킹과 일반 코드 유지를 확인했다.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 355 통과, `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 구현: `packages/scanners/src/cmp_scanners/`(`source.py`, `masking.py`, `envfile.py`)를 `pyproject.toml`에 등록했다. 설정 `ANALYSIS_WORKSPACE_ROOT`와 `.env.example` 항목을 추가했다.
- 문서 변경: 작업 공간 위치·재사용 범위 결정은 설계 변경 기록에 반영했다. spec·plan 변경 없음(AV-002·AV-020 범위 안).

- 반영 경위: 브랜치의 upstream이 `origin/main`으로 잡혀 있어 PR 없이 `main`에 직접 push됐다. `main`의 push CI는 통과했다. PR 리뷰 기록은 없다.

## 한계와 미검증
- 02의 `fetch_source` Activity는 mock 그대로다. 저장소·커밋·토큰 입력이 Activity에 들어오는 경로(`AnalysisRequest.source_access_ref`의 해석)가 정해지지 않아 연결은 후속(07·15)에서 한다.
- 운영 커밋 조회는 `resolve_production_commit` 인터페이스와 기본 stub(없음)만 있다. 실제 OCI 라벨 조회는 06에서 연결한다.
- 토큰은 환경 변수 기반 `http.extraheader`로만 전달하고 `.git/config`에 저장하지 않는다. 실제 GitHub 인증은 검증하지 못했다.
- 마스킹은 알려진 형태와 엔트로피 기준(24자 이상, 영문·숫자 혼합, 4.0 이상)이다. 짧은 비밀번호, 40·64자 순수 16진수, 코드에 하드코딩된 `.env` 값의 복사본은 놓칠 수 있다. 분석 단계는 `.env` 값을 읽지 않으므로 값 기반 대조는 하지 않는다.
- 같은 Worker 안에서만 재사용하며, 같은 커밋을 동시에 요청하면 이름 변경이 실패할 수 있다. 작업 공간 정리는 구현하지 않았다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: `.env` 값은 Sandbox 워커만 Secret 생성 때 읽는다는 결정 반영(이 티켓은 이름만).
- 2026-11-05(사용자 결정): 작업 공간은 설정 `ANALYSIS_WORKSPACE_ROOT`(없으면 OS 임시 디렉터리) 아래 `<migration_id>/<commit_sha>/`에 두고 같은 Worker 안에서만 재사용한다(서비스 디렉터리와 겹치면 거절, 권한 0700). 여러 Worker로 나뉘면 재클론한다. 작업 공간 정리는 구현하지 않는다(D-06·11에서 결정). S3 업로드는 09에서 한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
