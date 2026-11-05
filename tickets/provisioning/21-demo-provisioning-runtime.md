# Ticket 21 — 데모 플랫폼에서 Provisioning 실행 환경 구성

## 상태
TODO — [Issue #100](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/100). 착수 전 검토 필요. 2026-11-05 Provisioning 공백 점검에서 발견했다.

## 목적
Provisioning 코드는 로컬·k3d·실제 클라우드에서 검증됐지만, 데모 서버(`infra/demo`)에서는 실행할 수 없다. worker 이미지에 실행 도구가 없고, 승인·실행 Activity가 업무 DB와 산출물에 접근하지 못하며, 온프레미스 Agent가 붙을 공개 HTTPS 주소가 없다. 데모 서버에서 Plan 생성부터 세 대상 실행까지 실제로 돌게 한다.

## 관련 요구사항
PROV-008, PROV-009, PROV-015, PROV-016, PROV-021

## 선행 작업
Blocked by: 15 (DONE). 18과 병렬 가능. 공개 주소·TLS 방식은 Traffic(중계 서버) 구성과 겹치지 않는지 확인.

## 현재 상태 (`infra/demo`)
- worker 이미지(`python:3.11-slim`)에 terraform·helm·kubectl·aws CLI·gcloud 없음
- worker에 `DATABASE_URL` 없음 → 승인·실행 Activity가 업무 DB에 접속 불가
- 산출물 저장소: 로컬 디렉터리(`ARTIFACT_STORE_DIR`, `provisioning_artifact_root`)만 구현, API·worker가 공유하지 않음. S3 미구현
- API가 `127.0.0.1:8000`으로만 열림, HTTPS 없음 → Agent 접속 불가
- `APPROVAL_ADMIN_KEY`·Agent 관리자 키·클라우드 자격 증명 설정 없음

## 작업 범위
- 포함
  - worker 이미지에 버전·체크섬 고정한 terraform·helm·kubectl·aws CLI·gcloud 추가
  - worker·API 공통 설정: `DATABASE_URL`, 관리자 키, 공유 산출물 저장소(공유 볼륨 또는 S3 중 결정), Provisioning 산출물 경로
  - 클라우드 자격 증명 주입 방식(EC2 인스턴스 역할·GCP 서비스 계정 키 파일 마운트 등). 저장소·이미지에 자격 증명을 넣지 않는다
  - Agent용 공개 HTTPS 진입점(`/agent/*`만 외부 공개, 관리자·승인 API는 제한)
  - 데모 실행·점검 문서와 스모크 절차
- 제외: Plan 생성 로직(18), 중계 서버·Envoy·DNS(Traffic), 프론트 배포, 운영급 고가용성

## 수용 기준
- [ ] 데모 서버의 worker가 terraform·helm·kubectl·aws·gcloud를 실행하고, 승인·실행 Activity가 업무 DB와 산출물에 접근한다.
- [ ] 노트북의 Agent(15 Chart)가 공개 HTTPS 주소로 등록·작업 수신에 성공한다.
- [ ] 관리자·승인 API는 외부에 공개되지 않거나 인증으로 보호된다.
- [ ] 자격 증명·키가 이미지·저장소·로그에 없다.
- [ ] 데모 서버에서 스모크(Plan 게시 → 승인 → ON_PREM 실행)가 통과한다(18 완료 후).

## Test Seam 후보
이미지 빌드 후 도구 버전 확인, compose 설정 테스트(필수 환경변수·포트 노출 검사), 데모 서버에서 Agent 접속·스모크 실행.

## 검토 시 정할 것
- 산출물 저장소: 공유 볼륨 유지 vs S3 구현
- 공개 HTTPS 방식(ALB+ACM, Caddy 자동 인증서 등)과 도메인
- 클라우드 자격 증명 주입 방식과 실제 Apply 비용·정리 책임

## 관련 문서
- [15](15-agent-runtime-install.md) · [18](18-onprem-plan-publish.md) · [`infra/demo/README.md`](../../infra/demo/README.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
