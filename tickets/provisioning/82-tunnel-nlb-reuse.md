# Ticket 82 — 데이터 터널 원본 NLB를 고정 이름으로 재사용하고 미리 만든다

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰·고정 NLB 생성·데모 리허설 대기) — [Issue #326](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/326). 2026-11-07 사용자 지시로 착수. 담당 민서. **데이터 영역 코드(`cmp_data.tunnel`, `data_tunnel` Activity) 대행 — 데이터 담당 공유 필요.**

## 배경 (리허설, 2026-11-07 `migration-941df27a`)
- `ingress="source"`는 실행마다 원본 EKS에 `cmp-wg-<sha8>-endpoint` Service(LBC 공개 NLB, IP 대상, TCP 8080 헬스)를 만들고 `dm_tunnel_cleanup`에서 지운다.
- 새 NLB는 provisioning 2~3분 + 헬스체크(기본 10초×3) + DNS 전파(81)로 통로 준비가 3~4분이다. 941df27a의 NLB `k8s-cmpdatat-cmpwg846-…`은 05:35:06 생성 뒤 계속 provisioning이었다.
- 사용자 결정: 고정 NLB를 재사용하고 시연 전에 미리 만든다.
- 덤: worker 로그에 `coroutine '_ActivityWorker._heartbeat_async' was never awaited`가 반복된다. `establish`/`cleanup`이 `asyncio.to_thread` 안에서 `activity.heartbeat`를 부르는데, async Activity heartbeat는 이벤트 루프에서 task를 만들므로 스레드에서는 `RuntimeError`(삼켜짐)로 **heartbeat가 실제로 보내지지 않았다**.

## 작업 범위
- 포함: `WireGuardTunnel.standing_ingress`, `endpoint_manifest(standing=)`·`standing_endpoint_manifest`, 원본 NLB 헬스체크 annotation, `establish`/`cleanup`, 스레드 heartbeat, `scripts/data_tunnel_nlb.py`·make 대상, 데모 문서, DM-011 명세·계획 한 줄
- 제외: 실제 NLB 생성·삭제, 데모 서버 `.env`·worker 재배포, 지난 실행 잔여물 정리(별도 승인), Workflow·HTTP 계약(입력은 터널 dict를 그대로 넘긴다)

## 착수 결정 (2026-11-07, Issue #326)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 입력 | `tunnel.standing_ingress: bool = false`, `ingress="source"`일 때만 허용 | 기본값이면 기존 동작 그대로. target 입구(GKE)는 필요가 없다 |
| D2 | 이름·범위 | `cmp-data-tunnel/cmp-wg-standing-endpoint` 하나(원본 클러스터당) | 실행별 라벨(`cmp.dev/data-tunnel`) 대신 `cmp.dev/data-tunnel-standing: "true"` — 어느 실행의 자원으로도 보이지 않게 |
| D3 | 실행마다 | 같은 SSA field manager(`cmp-data-migration`)로 apply: selector=이번 `cmp.dev/data-tunnel=<name>`, `loadBalancerSourceRanges`=`target_egress_cidrs` | 있으면 갱신, 없으면 생성. 게이트웨이·키 Secret·NetworkPolicy·ConfigMap은 지금처럼 실행마다 새로 만든다(지난 키·wg0가 섞이지 않게) |
| D4 | 정리 | standing이면 입구 Service를 지우지 않는다(나머지 9개는 그대로 삭제) | 게이트웨이가 없으면 NLB 대상이 없다 |
| D5 | 미리 만들기 | 실행 없는 selector `cmp.dev/data-tunnel=standing-idle`. `up`은 Service 주소 → `aws elbv2` `active` → DNS 풀림까지 기다린다. 실행이 게이트웨이를 고르는 중이면 `up`/`down` 거절(`--force`) | 시연 중 실수로 selector·허용 대역을 되돌리지 않게 |
| D6 | 헬스체크 | 간격 5초, 제한 4초, healthy·unhealthy 임계 2 | LBC annotation(`healthcheck-interval`·`-timeout`·`-healthy-threshold`·`-unhealthy-threshold`). NLB 허용 범위 간격 5~300, 제한 2~120, 임계 2~10. 제한은 간격보다 짧게 |
| D7 | heartbeat | 비동기 Activity가 스레드로 넘기기 전에 이벤트 루프를 ContextVar에 두고, 스레드의 `_beat`는 `loop.call_soon_threadsafe(ctx.run, activity.heartbeat, …)` | `asyncio.to_thread`가 문맥을 복사하므로 스레드에서 루프·Activity 문맥이 보인다 |
| D8 | 동시 실행 | 고정 NLB는 한 번에 한 실행만 | 데모 전제. 두 실행이 겹치면 나중 실행이 selector를 가져간다 |

## 테스트 Seam
- `tests/data/test_data_tunnel_standing.py`: 계약 검증, 고정 매니페스트, `establish`(가짜 클러스터)가 고정 Service에 selector·허용 대역을 넣고 그 이름으로 주소를 기다림, `cleanup`이 고정 Service를 남김, `ActivityEnvironment`에서 heartbeat가 루프 스레드에서 도착.
- `tests/data/test_data_tunnel_nlb_script.py`: 스크립트의 kubectl·aws·DNS를 가짜로(적용 매니페스트, 대기 순서, 사용 중 거절, 시간 초과, 삭제 대상).
- 실제 NLB·LBC 반영은 승인 뒤 원본 EKS에서 확인.

## 수용 기준
- [x] 기본(`standing_ingress` 없음)은 기존과 같다(실행별 NLB 생성·삭제)
- [x] standing이면 고정 이름 Service를 apply(selector·허용 대역 갱신), 그 주소를 쓴다, 정리 때 남긴다
- [x] target 입구 + standing은 거절
- [x] 원본 NLB 헬스체크 단축 annotation
- [x] 미리 만들기·확인·지우기 스크립트와 `make data-tunnel-nlb-up|status|down`, 데모 문서 절차
- [x] 스레드 heartbeat가 루프에서 보내진다(`never awaited` 경고 원인 제거)
- [ ] 원본 EKS에 고정 NLB 생성, 데모 서버 `.env` 반영·worker 재배포, 리허설에서 통로 준비 시간 확인(별도 승인)

## 검증
`make check` 통과. OpenAPI·alembic 영향 없음(HTTP 경로·DB 변경 없음). 데이터 담당에게 `cmp_data.tunnel` 변경 공유 필요.
