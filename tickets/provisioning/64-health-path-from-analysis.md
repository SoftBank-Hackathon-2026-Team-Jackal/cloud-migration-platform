# Ticket 64 — 앱 헬스 경로를 분석 결과에서 트래픽 인계까지 잇기

## 상태
IN_PROGRESS (로컬 구현·검증 완료, PR 리뷰 대기) — [Issue #280](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/280). 2026-11-07 조율 세션 지시로 착수. 담당 민서.

## 결론
- 데모 chat-app(`/healthz`)은 **이미** 매니페스트 readinessProbe에서 `/healthz`를 읽어 Plan → 구축 결과 → 트래픽 인계까지 그대로 간다. 점검에서 걱정한 "`/health`로 확인해 `TARGET_UNHEALTHY`"는 Plan에 값이 있으면 생기지 않는다. 이번 티켓은 이를 테스트로 고정했다.
- 빈틈은 **probe가 없는 앱**이었다. 그때 분석은 코드에 `GET /healthz`가 있어도 `/`를 썼다. 이제 코드 라우트에서 헬스 이름의 경로를 찾아 쓴다.

## 경로 우선순위 (분석 `build_requirements`)
1. 컨테이너 순서대로 readinessProbe, 그다음 livenessProbe의 `httpGet.path`(기존 동작)
2. (새로) 코드 라우트(`scan_go_routes`) 중 GET(또는 메서드 없음)이고 경로가 정확히 `/healthz`·`/health`·`/readyz`·`/livez`·`/ready`·`/ping` 중 하나인 것(이 순서로). 쓰면 가정 `HEALTH_PATH_FROM_CODE_ROUTE: <리소스> uses <경로> (<파일>:<줄>)`을 남긴다.
3. 모두 없으면 기본값 `/`(가정 `HEALTH_PATH_NOT_DECLARED`)

샌드박스 리허설은 같은 요구사항(`AppApi.health_path = apps[0].health_path`)을 쓰므로 따로 우선순위를 두지 않는다. Fixer가 고친 요구사항은 새 `requirements_ref`로 Plan에 들어간다.

## 결정
| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| D1 | 새 필드 | 만들지 않는다. 요구사항 `health_path` → Plan `application.health_path` → refs `app_health_path` → 결과 `application.health_path` → `TrafficHandover.health_path`를 그대로 채운다 | 계약이 이미 있음 |
| D2 | 분석 기본값 | `/` 유지 | 모르는 앱은 `/`가 200일 가능성이 더 높다. 판정(`HEALTH_PATH_NOT_FOUND` 소견)은 그대로 |
| D3 | 인계 기본값 | `/health` 유지 (`DEFAULT_HEALTH_PATH`) | Traffic `Handover` 기본값과 같아야 한다(계약 테스트). Plan에 값이 항상 있어서 정상 흐름에서는 쓰이지 않고, 예전 결과처럼 refs에 `app_health_path`가 없을 때만 쓰인다 |
| D4 | ON_PREM·클라우드 | 같은 값. ON_PREM은 `resource_refs`가, AWS/GCP는 `k8s_workloads`가 Plan 값을 `app_health_path`로 쓴다 | 테스트로 세 제공자 고정 |

## 바꾸지 않은 것
분석 판정·트래픽 판정 로직, 계약 필드, 웹.

## 검증
- `tests/test_health_path_chain.py`: demo-app 모양(probe `/healthz`) → 요구사항 `/healthz`, probe 우선, probe 없으면 코드 라우트 `/healthz`, POST만 있으면 무시, 둘 다 없으면 `/`. Plan 요청 `/healthz`. AWS·GCP·ON_PREM 인계 `/healthz`, 값 없으면 `/health`.
- 전체 `pytest`, `ruff check`, `ruff format --check`.
- 못 본 것: 실제 클러스터·중계에서의 확인(배포 뒤 데모 E2E에서 본다).
