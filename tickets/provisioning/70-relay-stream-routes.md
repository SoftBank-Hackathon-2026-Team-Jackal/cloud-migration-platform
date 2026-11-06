# Ticket 70 — 중계 서버 실시간 연결 경로(/api/stream)는 제한시간·재시도 없이

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·중계 서버 반영 대기) — [Issue #294](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/294). 2026-11-07 조율 세션 지시로 착수. 트래픽 영역(이상헌님) 코드이며 김민서 승인으로 대행한다.

## 배경
결선 데모에서 트래픽 전환 중 중계 서버(Envoy) 주소로 채팅 화면을 열면 실시간 연결 `/api/stream`(SSE, 2초 heartbeat)이 라우트 전체 제한시간 5초에 끊긴다. 끊김은 `upstream_rq_timeout`으로 집계되고, 전환 판정(`parse_cluster_stats`: failed = 5xx + timeout)이 실패로 세어 롤백 원인이 된다.

재현(main `f13dcb2`, 로컬 Envoy v1.32): 현재 설정은 5초에 끊기고 timeout 1건. `/api/stream` 전용 라우트(`timeout: "0s"`, 재시도 없음)를 `/` 앞에 두면 14초 동안 유지, timeout 0건, `/api/messages`는 200 그대로.

## 관련 요구사항
ADR-006(트래픽 전환·중계 서버), #62(Envoy 어댑터), #63(릴레이 워커), #200(데모 중계 서버).

## 작업 범위
- 포함
  - `build_route_config(..., stream_prefixes=())`와 경로 검증, `EnvoyProxy(stream_prefixes=...)`
  - 릴레이 워커 `TRAFFIC_STREAM_PREFIXES` 읽기(`build_ports`)
  - 데모 중계 서버 기본값(`TRAFFIC_STREAM_PREFIXES=/api/stream`)과 이미 깔린 서버 적용 스크립트 `infra/relay/enable-stream-routes.sh`
- 제외
  - 전환 판정식(5xx + timeout) 변경, 관찰 창·정책 기본값 변경
  - 실제 중계 서버 반영(병합 뒤 조율 세션이 lib 반영·relay-worker 재시작)
  - 웹·API·Workflow 변경

## 착수 결정 (2026-11-07, Issue #294)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 라우트 위치 | prefix마다 라우트 하나, `/admin`·`/__cmp` 403 뒤, `/` 앞 | 막힌 경로를 먼저 거르고, Envoy는 먼저 맞는 라우트를 쓴다 |
| D2 | 라우트 내용 | `/`와 같은 `weighted_clusters`(응답 헤더·`host_rewrite_literal` 포함), `timeout: "0s"`, `retry_policy` 없음 | 실시간 연결도 전환 비율을 따른다. 재시도는 이미 응답을 시작한 스트림에 의미가 없고 0s와 함께 쓰면 오래 붙잡힌다 |
| D3 | 기본값 | 코드 기본값은 빈 값(출력이 예전과 완전히 같음). 데모 중계 서버만 relay-worker 환경으로 `/api/stream` | 앱마다 실시간 경로가 다르다. 범위를 데모 앱에 한정 |
| D4 | 검증 | `/`로 시작해야 하고 `/admin`·`/__cmp`와 양방향으로 겹치면 거절(`/`, `/a`, `/admin/x`, `/__cmpx`). 중복은 하나로 | `/`를 받으면 모든 요청이 제한시간 없이 나간다 |
| D5 | 잘못된 환경값 | `build_ports`가 `RelayNotConfigured`(재시도 안 함)로 실패 | 다시 해도 같은 설정 오류다. 기존 `TRAFFIC_RDS_PATH` 누락과 같은 코드 |
| D6 | 반영 시점 | RDS를 쓸 때마다(가중치 적용·롤백) 들어간다. 이미 깔린 서버는 relay-worker 재시작 뒤 다음 가중치 적용 때 반영 | 별도 RDS 재작성 경로를 만들지 않는다 |

## 공개 인터페이스

```python
# cmp_traffic.envoy
BLOCKED_PREFIXES = ("/admin", "/__cmp")
check_stream_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]  # ValueError
build_route_config(weights, version, target_host=None, stream_prefixes=()) -> dict
EnvoyProxy(..., stream_prefixes: Sequence[str] = ())
# cmp_worker.activities.traffic
stream_prefixes_from_env() -> tuple[str, ...]   # TRAFFIC_STREAM_PREFIXES, 쉼표 구분
```

## 테스트 Seam
- 순수: `build_route_config` 출력(경로 순서·timeout·retry·가중치·host_rewrite), 기본값 출력 고정 비교, 검증 실패.
- 어댑터: 가짜 관리 API 뒤 `EnvoyProxy.apply`가 가중치를 바꿀 때마다 stream 라우트를 다시 쓰는지.
- 워커: 환경변수 → `build_ports()`의 `proxy.stream_prefixes`, 잘못된 값은 `RelayNotConfigured`.
- 인프라: `enable-stream-routes.sh` 기본값 `/api/stream`·`bash -n`, `setup-relay.sh` 기본값 안내.

## 수용 기준
- [x] stream prefix 라우트가 `/` 앞, timeout `0s`, retry 없음, 가중치·host_rewrite가 `/`와 같다
- [x] 기본값(빈 값)에서 라우트 출력이 예전과 같다
- [x] 가중치를 바꿀 때마다 RDS에 stream 라우트가 남는다
- [x] `TRAFFIC_STREAM_PREFIXES` 파싱, 잘못된 값은 재시도 없이 실패
- [x] 데모 중계 서버 기본값과 이미 깔린 서버 적용 방법(`infra/relay`)
- [x] `make check`
- [ ] 데모 중계 서버 반영 뒤 전환 중 `/api/stream` 유지·timeout 0건 확인(조율 세션)

## 문서
ADR-006 한계 항목에 실시간 연결 경로 한 줄. API·Workflow·계약 변경 없음(openapi·alembic 변경 없음).
