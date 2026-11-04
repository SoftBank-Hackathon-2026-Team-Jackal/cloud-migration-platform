# Ticket 17 — 코드 색인과 에이전트 조회 도구

## 상태
DRAFT — 사용자 검토 전 · **후순위**(첫 사이클 완료 후 착수)

## 목적
에이전트가 원본 파일을 통째로 읽지 않고 파싱해 둔 구조를 조회하게 해, 토큰·시간을 줄이고 감싼 호출(헬퍼 함수 안의 SDK 호출 등)까지 정확히 찾는다.

## 관련 요구사항
AV-003, AV-007, AV-020

## 선행 작업
Blocked by: 05, 08

## 작업 범위
- 포함: 05 스캐너 결과를 코드 색인(심볼, 호출 그래프, 환경변수 읽기, 라우트, import)으로 저장, 파일 해시별 캐시와 바뀐 파일만 다시 파싱, 에이전트 도구(`find_calls`, `callers_of`, `env_readers`, `routes`, `snippet`, `resource_graph`), Mapper·Tester·Fixer에 도구 연결, 카탈로그 `detect_rules`의 패턴 형식(semgrep / ast-grep / 자체 AST) 결정
- 제외: 벡터 임베딩 검색(RAG), 동적 호출 해석(13번 출구 기록이 담당)

## 수용 기준
- [ ] 헬퍼 함수 안에서 만든 boto3 클라이언트의 실제 사용 지점을 `callers_of`로 찾는다(grep 기준선이 놓치는 fixture).
- [ ] 도구가 돌려주는 위치가 그대로 `Evidence.locator`로 쓰이고 `check_evidence`를 통과한다.
- [ ] 같은 분석에서 LLM 입력 토큰이 파일 단위 입력 대비 줄어든 수치를 기록한다.
- [ ] Fixer 수정 후 바뀐 파일만 다시 파싱한다.
- [ ] `snippet`이 마스킹본만 돌려준다.
- [ ] 새 의존성은 분석 Worker 전용 그룹으로 두고 PR에 적는다.

## 테스트 전략
- 제안 Test Seam: 색인 조회 도구의 공개 함수 결과. 잡는 것: 호출 그래프 추적·캐시·마스킹. 못 잡는 것: 동적으로 만든 서비스 이름.
- 실행: 감싼 호출이 있는 fixture 앱, `uv run pytest tests/analysis/test_code_index.py`, 토큰 수 비교 기록

## 완료 시 확인할 동작
Mapper가 도구 호출만으로 매핑 근거를 모으고, 결과가 기존 매핑과 같거나 더 많은 사용 지점을 찾는다.

## 결정 대기
- 탐지 규칙 형식(첫 사이클 지원 언어 결정 후, D-07)
- LLM 전송 범위를 "근거 주변 코드 조각"으로 ADR에 기록(D-01)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)

## 설계 변경 기록
- 2026-11-03: 파싱 기반 색인·조회 도구를 후순위 티켓으로 추가. 외부 계약 변경 없음(분석 내부 구현).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
