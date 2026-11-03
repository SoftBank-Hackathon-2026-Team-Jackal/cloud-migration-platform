# Ticket 04A — 온프레미스 Agent 등록과 공통 K3s 배포 계약

## 상태

DONE — [Issue #20](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/20). 2026-11-03 사용자 대화에서 범위와 공개 Test Seam을 검토했다.

## 목적

기존 로컬 Agent 검증(04)과 DB·앱 공통 Chart(02·03)를 Sandbox 및 사용자 제공 K3s 운영 경계에 연결할 준비를 한다. 회원가입을 추가하지 않고 Agent 소유자를 식별한다.

## 관련 요구사항

PROV-005, PROV-006, PROV-009

## 선행 작업

Blocked by: 02, 03, 04 (DONE). 운영 Apply는 07과 12에서 연결한다.

## 범위

- 최소 소유자 기록과 그 소유자에 묶인 Agent 등록. 일회용·만료 등록 토큰을 사용하고 원문을 저장하지 않는다.
- Agent의 플랫폼 방향 아웃바운드 등록·인증·자기 작업 조회 경계를 만든다. 다른 소유자·Agent 작업은 읽거나 완료 보고할 수 없다.
- [공통 배포 계약](../../specs/provisioning/onprem-shared-deployment.md)의 입력을 검증하고 기존 DB/App Chart·바인딩을 재사용해 Values·Manifest 및 내부 DB 주소/환경변수 참조를 반환한다.
- 기존 로컬 Agent 경로의 회귀를 유지한다.

## 제외

- 공개 회원가입·사용자 로그인, Sandbox의 배포·기능 테스트·정리, 신규 K3s 클러스터 구축.
- 승인된 Production Apply와 Ready 판정(12), 승인 게이트(07), 외부 Ingress·WireGuard·Envoy 수명 관리.

## 수용 기준

- [x] Agent 등록은 유효한 일회용 토큰과 소유자 바인딩으로만 성공한다. 만료·재사용·다른 소유자 사용은 거절한다.
- [x] 등록된 Agent만 자기 작업을 조회·보고한다. 등록 토큰과 인증 값의 원문은 DB·작업·로그에 남기지 않고, 발급·등록 전용 응답에서만 한 번 반환한다.
- [x] SANDBOX/PRODUCTION의 같은 공통 입력 형식을 받아 DB/App Values·Manifest, 계산된 DB Service 주소와 앱 환경변수/Secret 참조를 반환한다. 기존 Chart를 복제하지 않는다.
- [x] 렌더 결과를 배포 성공으로 표시하지 않으며 비정상 입력은 구조화 오류로 거절한다.
- [x] SDD·티켓·테스트 증거가 구현과 일치한다.

## Test Seam

등록·인증/작업 조회의 공개 API 또는 서비스 경계와 공통 렌더 함수의 입력·출력을 검사한다. 토큰 재사용·만료·소유자 간 접근, 필수 필드 누락·잘못된 DB 바인딩을 실패로 확인한다. 이는 외부 NAT·실제 사용자 인증, 승인된 운영 K3s Apply·Ready를 증명하지 않는다. 격리 K3s 로컬 실배포 증거는 04에 남긴다.

## 완료 증거

`tests/test_agent_enrollment.py`, `tests/test_remote_agent.py`, `tests/test_shared_deployment.py`에서 일회용 등록, 만료·소유자 격리, 아웃바운드 전송, 두 환경의 Helm Manifest와 오류를 확인했다. 전체 `.venv/bin/pytest -q`는 209개 통과했고 `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff --check`도 통과했다. SQLite 격리 DB에 `alembic upgrade head`를 적용한 뒤 `alembic check`는 차이를 발견하지 않았다. `.venv/bin/python scripts/export_openapi.py`로 HTTP 계약을 재생성했다.

이 실행 환경에서는 `uv`가 macOS system-configuration 조회 중 panic하여 `make check`와 `make openapi`가 시작되지 않았다. 위의 직접 실행 명령은 두 Make 타깃과 같은 도구·인자를 사용한다. 실제 외부 HTTPS·NAT 연결과 승인된 운영 K3s Apply·Ready 검증은 이 티켓의 증거가 아니며, 후자는 07·12에서 다룬다. 관련 SDD 변경은 제품 정책 P-11, 아키텍처 경계·Temporal 흐름, Provisioning spec/plan/shared-contracts, OpenAPI, 공통 배포 계약과 README에 반영했다.
