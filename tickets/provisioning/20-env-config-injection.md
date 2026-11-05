# Ticket 20 — 업로드 `.env`를 대상 ConfigMap·Secret으로 생성

## 상태
TODO — [Issue #99](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/99). 착수 전 검토 필요. 2026-11-05 Provisioning 공백 점검에서 발견했다.

2026-11-05: 분석 담당과 `.env` 규칙을 합의해 [.env 단일 참조 명세](../../specs/env-file-ref/spec.md)로 구체화했다. 구축 쪽 구현은 [ENV-04](../env-file-ref/04-provisioning-render-env.md)로 진행하고 이 Issue(#99)를 그대로 쓴다. 업로드·공통 함수·시작 요청은 ENV-01~03이다.

## 목적
운영 배포(09·11·12)는 앱이 참조하는 ConfigMap과 사용자 Secret이 대상에 미리 있어야 통과한다. 없으면 AWS/GCP는 `CONFIG_MAP_MISSING`·`USER_SECRET_MISSING`, ON_PREM은 `SECRET_REFERENCE_MISSING`으로 실패한다. 지금은 이 값을 만드는 코드가 없고, 실검증 때는 사람이 직접 만들었다. 사용자가 업로드한 `.env`를 일반 설정(ConfigMap)과 비밀값(Secret)으로 나눠 대상에 만들어, 사람 손 없이 배포가 이어지게 한다.

## 관련 요구사항
PROV-004, PROV-012, PROV-016, PROV-021

## 선행 작업
Blocked by: 09, 11, 12, 19 (DONE), 18(승인된 Plan에 `.env` 참조가 들어가야 함). 분석 쪽 `.env` 업로드·분류 결과 형식은 승철님과 확인.

## 현재 구현
- `cmp_scanners.envfile`: `.env`의 변수 이름만 파싱(분석). 값 저장·전달 경로 없음
- Values의 `config_ref`·`secret_ref`: 대상에 이미 있는 ConfigMap·Secret 이름만 참조
- 배포 Activity·Agent: 참조 대상의 존재만 확인

## 작업 범위
- 포함
  - 승인된 Plan의 `.env` 산출물 참조(Digest 고정)를 실행 시점에 읽어, 일반 설정은 ConfigMap, 비밀값은 Secret으로 대상 Namespace에 만들거나 갱신한다(플랫폼 관리 라벨, 관리 밖 같은 이름이면 덮어쓰지 않고 `SECRET_NAME_CONFLICT`).
  - AWS/GCP 배포 Activity와 ON_PREM Agent 작업에 같은 규칙을 적용한다. ON_PREM 비밀값은 작업 본문이 아니라 별도 인증 경로로 Agent가 받는다.
  - Plan·Preview에는 변수 이름·분류·참조만 넣고 값은 넣지 않는다.
  - 누락 변수와 잘못된 참조를 구조화 오류로 식별한다.
- 제외: `.env` 업로드 UI·저장(분석·Backend), Sandbox 쪽 주입(분석 11), 외부 Secret Manager 연동

## 수용 기준
- [ ] 승인된 `.env` 참조로 세 대상에 ConfigMap·Secret이 만들어지고, 사람이 미리 만들지 않아도 09·11·12 배포가 성공한다.
- [ ] 비밀값이 Values·Plan·Preview·작업 본문·결과·로그·Temporal 이력에 없다.
- [ ] 승인 후 `.env`가 바뀌면 Digest 불일치로 실행하지 않는다.
- [ ] 관리 밖 같은 이름 ConfigMap·Secret은 덮어쓰지 않는다.
- [ ] 기존 경로(참조만 하는 Plan) 회귀 없음.

## Test Seam 후보
09·11의 가짜 ClusterSession, 12의 가짜 클러스터로 생성·갱신·충돌·누락. 비밀값 노출 검사는 19와 같은 방식(결과·trace·로그 문자열 검사).

## 검토 시 정할 것
- 업로드된 `.env` 값의 저장 위치·형식과 일반/비밀 분류 주체(분석 결과를 그대로 쓸지, 사용자 확인을 거칠지)
- ON_PREM Agent가 비밀값을 받는 경로(전용 인증 API 또는 작업별 일회성 조회)
- ConfigMap·Secret 이름 규칙

## 관련 문서
- [09](09-aws-workloads.md) · [11](11-gcp-workloads.md) · [12](12-k3s-approved-rollout.md) · [19](19-db-connection-url.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
