# Tasks: .env 단일 참조와 공통 환경변수 해석

2026-11-05 사용자(민서)와 분석 담당(박승철님)의 합의에 따른 작업이다. 공통 함수·업로드 API·구축은 민서가 구현하고, 분석·샌드박스 담당자는 공통 함수를 가져다 쓴다. ENV-03은 착수 결정을 기록하고 진행 중이다. 나머지는 담당자 검토 뒤 진행한다.

| ID·티켓 | 담당 | 상태 | 선행 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [ENV-01 .env 업로드 API·암호화 저장·삭제 정책](../../tickets/env-file-ref/01-env-upload-store.md) | 민서 | DRAFT | — (결정 필요 2·3) | [#145](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/145) | ☐ |
| [ENV-02 시작 요청 env_file_ref와 Workflow 전달](../../tickets/env-file-ref/02-migration-request-env-ref.md) | 민서 | DRAFT | ENV-01, #60과 조율 | [#146](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/146) | ☐ |
| [ENV-03 공통 함수 resolve_env_names·render_env](../../tickets/env-file-ref/03-env-common-functions.md) | 민서 | IN_PROGRESS | ENV-01(저장소 인터페이스, 이 티켓이 Protocol을 정함) | [#147](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/147) | ☐ |
| [ENV-04 구축(v2)을 render_env로 전환](../../tickets/env-file-ref/04-provisioning-render-env.md) | 민서 | DRAFT | ENV-02, ENV-03, ENV-05 | [#99](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/99) 재사용 | ☐ |
| [ENV-05 분석: env_file_ref에서 키 읽기·EnvPlan 출력](../../tickets/env-file-ref/05-analysis-env-plan.md) | 박승철님 | DRAFT | ENV-02, ENV-03 | 미생성 | ☐ |
| [ENV-06 샌드박스: render_env 사용](../../tickets/env-file-ref/06-sandbox-render-env.md) | 샌드박스 담당 | DRAFT | ENV-03, ENV-05 | 미생성 | ☐ |
| [ENV-07 Fixer가 추가한 변수 처리](../../tickets/env-file-ref/07-fixer-added-env.md) | 미정 | DRAFT | ENV-05 | 미생성 | ☐ |

## 선행 관계

```text
ENV-01 ─┬─ ENV-02 ─┬─ ENV-05 ─┬─ ENV-04
        └─ ENV-03 ─┘          ├─ ENV-06
                              └─ ENV-07
```

ENV-01과 ENV-03은 저장소 인터페이스만 먼저 맞추면 병렬로 진행할 수 있다. ENV-05는 ENV-03의 `resolve_env_names`와 계약(EnvPlan)만 있으면 시작할 수 있다.

## 공통 수용 기준

- 원문 값이 응답·로그·Temporal 입력과 이력·Plan·Preview·이벤트·운영 로그에 없다.
- 샌드박스와 구축이 같은 `env_file_ref`와 같은 EnvPlan을 쓰고, `render_env` 호출의 차이는 DB 출력뿐이다.

## 결정 필요

[spec.md의 Open questions](spec.md#open-questions) 2~4. 2·3번은 ENV-01, 4번은 ENV-07에서 막힌다. 1번은 2026-11-05 결정됨(승인 전 차단, [ENV-03 착수 결정](../../tickets/env-file-ref/03-env-common-functions.md#착수-결정-2026-11-05-사용자-승인) D1).

## 기존 티켓과의 관계

- Provisioning 20(#99, `.env`를 대상 ConfigMap·Secret으로 생성)은 이 작업으로 구체화된다. 구축 쪽 구현은 ENV-04로 진행하고 #99를 그대로 쓴다.
- #60(공개 GitHub 저장소를 분석 소스 입력에 연결)도 `MigrationRequest`와 Workflow 입력을 바꾼다. ENV-02는 같은 요청 모델에 필드만 더하므로, 먼저 병합되는 쪽에 맞춰 다른 쪽이 따라간다.
