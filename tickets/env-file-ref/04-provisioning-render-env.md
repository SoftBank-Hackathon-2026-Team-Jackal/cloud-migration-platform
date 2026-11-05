# ENV-04 — 구축(v2)을 render_env로 전환

## 담당
민서 (Provisioning 20, [#99](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/99) 재사용)

## 상태
DRAFT

## 목적
운영 구축이 사람이 미리 만든 ConfigMap·Secret 없이, 승인된 `env_file_ref`와 EnvPlan으로 대상에 앱 env Secret을 만들고 참조를 연결한다.

## 관련 요구사항
ENV-007, ENV-008. PROV-004, PROV-010, PROV-012, PROV-021.

## 선행 작업
ENV-02, ENV-03, ENV-05(EnvPlan). Provisioning 09·11·12·18·19·22 (DONE).

## 현재
`plan_publish`가 `environment.env_file_ref`를 앱 Values의 `config_ref`로 넘길 뿐, 값을 대상에 만들지 않는다. 대상에 없으면 `CONFIG_MAP_MISSING`·`USER_SECRET_MISSING`(AWS/GCP), `SECRET_REFERENCE_MISSING`(ON_PREM)으로 실패한다.

## 범위
- 포함
  - Plan 게시: EnvPlan을 Plan 입력 Digest에 넣고, Preview에는 키 이름·처리 결과만 표시한다. `decisions`가 있으면 결정 필요 1번 정책대로 처리한다.
  - AWS/GCP 배포 Activity: `render_env(EnvPlan, env_file_ref, 대상 DB 출력)` 결과로 `<앱 release>-env` Secret을 만들거나 갱신하고(플랫폼 관리 라벨, 관리 밖 같은 이름은 `SECRET_NAME_CONFLICT`), 앱 Values env를 `secretKeyRef`로 연결한다.
  - ON_PREM: Agent가 클러스터 안에서 같은 결과를 적용한다. 값은 작업 본문에 넣지 않고 Agent 인증 API로 작업별로 받는다.
  - 승인 뒤 `env_file_ref` 버전이 바뀌면 실행하지 않는다(Digest 불일치).
  - 티켓 20 문서를 이 티켓으로 연결, SDD 갱신
- 제외: 분석 EnvPlan 생성(ENV-05), 샌드박스(ENV-06)

## 수용 기준
- [ ] 사람이 미리 만든 ConfigMap·Secret 없이 09·11·12 구축이 성공한다.
- [ ] 샌드박스와 같은 `env_file_ref`·EnvPlan을 쓰고 DB 출력만 다르다.
- [ ] 원문 값이 Values·Plan·Preview·작업 본문·결과·이벤트·로그·Temporal 이력에 없다.
- [ ] 관리 밖 같은 이름 Secret은 덮어쓰지 않는다.
- [ ] `env_file_ref`가 없는 기존 Plan은 그대로 동작한다.

## Test Seam
09·11의 가짜 ClusterSession, 12의 가짜 클러스터, InMemory `.env` 저장소. 비노출은 19와 같은 문자열 검사.

## 결정 필요
- 1. `UNREPRESENTABLE`·기본값 없는 `MISSING`을 승인 전에 막을지, 사람이 값을 넣게 할지
