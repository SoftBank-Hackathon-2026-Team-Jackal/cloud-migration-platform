# Ticket 03 — 소스 확보·운영 커밋 고정·마스킹

## 상태
DRAFT — 사용자 검토 전

## 목적
분석할 소스를 격리된 작업 공간에 고정된 커밋으로 확보하고, 빌드용 원본과 LLM용 마스킹본을 분리한다.

## 관련 요구사항
AV-002, AV-020

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: 토큰 기반 클론(GitHub App은 후속), 실행 이미지 OCI 라벨에서 커밋 찾기(인터페이스 + stub), 작업 공간 재사용, 마스킹 함수, `.env` 이름만 파싱
- 제외: GitHub App 연동, Draft PR(14)

## 수용 기준
- [ ] 커밋을 못 찾으면 사용자 지정 커밋을 쓰고 `commit_verified=false`를 남긴다.
- [ ] 같은 커밋 재요청은 다시 클론하지 않는다.
- [ ] 마스킹본에 알려진 키 패턴·고엔트로피 문자열·`.env` 값이 남지 않는다.
- [ ] `.env` 파싱 결과에 값이 없고 이름만 있다.
- [ ] 작업 공간 경로가 Migration·커밋별로 분리되고 서비스 프로세스 디렉터리 밖이다.

## 테스트 전략
- 제안 Test Seam: `fetch_source`의 공개 함수 결과와 마스킹 함수 출력. 잡는 것: 커밋 고정·비밀 유입. 못 잡는 것: 실제 GitHub 권한·대용량 저장소 시간.
- 실행: fixture 저장소(로컬 bare repo)로 `uv run pytest tests/analysis/test_source.py`

## 완료 시 확인할 동작
fixture 저장소를 넣으면 커밋 고정된 원본·마스킹본과 `.env` 이름 목록이 나온다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
