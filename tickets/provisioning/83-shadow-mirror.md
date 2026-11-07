# Ticket 83 — 그림자 리허설 1/3: 중계 Envoy GET 복사 + 3-way diff 수집기 + shadow Activity (트래픽 영역 대행)

## 상태
IN_REVIEW (로컬 구현·시험·Envoy v1.32 컨테이너 확인 완료. PR 리뷰·중계 서버 반영 승인·이상헌님 공유 남음) — [Issue #362](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/362).

> **트래픽 영역 대행 — 이상헌님 공유 필요.** 중계 서버(Envoy 라우트·클러스터, relay-worker)는 트래픽 담당 영역이다. Provisioning(김민서)이 대행했다. 기본은 꺼짐이고, 끄면 라우트 출력이 지금과 바이트 단위로 같다.

> 파일 번호: 같은 83 번호의 [83-tunnel-standing-gateway](83-tunnel-standing-gateway.md)가 이미 있다. 이 작업은 오케스트레이터가 정한 이름(`83-shadow-mirror`)을 따른다.

## 배경
결승 데모는 온프레미스(Mac k3d)로 실제 이전을 한다: 데이터 복제(CDC) → DB 전환(2차 승인, PgBouncer) → 트래픽(중계 Envoy 가중치) → 완료. 복제가 따라잡은 뒤 DB 전환 승인 **전**에, 중계 Envoy로 들어온 실제 GET을 대상에도 보내 "대상이 원본과 같은 답을 하는가"를 미리 본다. 결과는 경고·표시만 한다. 84(Workflow·이벤트·AI 설명)와 85(웹)가 아래 계약으로 병렬 진행한다.

## 절대 원칙
자동 차단·자동 수정·DB 설정 변경·쓰기 복사·운영 Pod 재시작을 하지 않는다. 기본 꺼짐.

## 작업 범위
- 포함
  - `cmp_traffic.envoy`: `ShadowMirror`, `build_route_config(..., shadow=None)`, `route_shadow`, `EnvoyProxy.set_shadow`, `cluster_address`
  - `cmp_traffic.relay.add_shadow_cluster`(bootstrap에 `cluster_shadow` 정적 추가)
  - `cmp_traffic.shadow`: 수집기 + 제어 API + 진입점(`python3.11 -m cmp_traffic.shadow`), 표준 라이브러리만
  - `cmp_worker.activities.traffic_shadow`: `shadow_start`·`shadow_summary`·`shadow_stop`, relay 큐 워커 등록
  - `infra/relay/enable-shadow.sh`(on·off·revert), `infra/relay/shadow-collector.service`
- 제외
  - Workflow·이벤트·판정(84), 웹(85), 중계 서버 실제 반영(승인 필요), migration 큐 등록(같은 호스트의 Envoy·수집기가 필요해 relay 큐에만 둔다)

## 공유 계약 (84·85와 고정)

```text
[relay 큐 Activity]
shadow_start({migration_id, run_id, allow_paths:[str], percent:int, rate_per_sec:int}) -> {"ok":bool, "code":str|null}
shadow_summary({run_id}) -> {"run_id","requests","compared","match","mismatch","skipped","target_5xx",
                             "origin_p99_ms":float|null,"target_p99_ms":float|null,"noise_fields":[str],
                             "samples":[{"method","path","status_origin","status_target",
                                         "diff":[{"pointer","origin","target"}]}]}
shadow_stop({run_id}) -> {"ok":bool, "code":str|null}   # 멱등. 수집기가 죽어도 Envoy 복사 끄기를 먼저 시도
실패 코드: SHADOW_UNAVAILABLE
[입력 — WEB_START_DATA_MIGRATION.shadow]
{"enabled":false,"window_s":180,"min_requests":50,"percent":100,"rate_per_sec":20,"pass_ratio":0.99,"allow_paths":[]}
```

계약 안의 해석(계약 변경 아님)
- `shadow_start`는 실패를 예외로 던지지 않고 `{"ok": false, "code": "SHADOW_UNAVAILABLE"}`을 돌려준다: 수집기 미응답·거절, 허용 경로가 비었거나 `/`·`/admin`·`/__cmp`와 겹침, `/`로 시작하지 않음, percent가 1~100 밖, 중계 서버 라우트 설정 없음, Envoy 적용 미확인. Envoy 적용에 실패하면 수집기 실행을 먼저 끝낸다.
- `shadow_summary`는 수집기에 닿지 못하거나 실행을 모르면 `ApplicationError(type="SHADOW_UNAVAILABLE", non_retryable=True)`.
- `shadow_stop`의 `ok`는 **Envoy 복사가 꺼졌는가**다. 이미 꺼져 있으면 파일을 다시 쓰지 않고 `ok: true`. 수집기가 죽어 있어도 Envoy가 꺼지면 `ok: true`.
- `rate_per_sec`는 **원본 재요청** 초당 상한이다. 비교 1건 = 원본 2번이므로 비교는 초당 `rate_per_sec/2`건까지(기본 20 → 10건). 2~100으로 자른다.
- `requests` = 실행 중 수집기가 받은 복사본 수, `compared` = 원본·대상 모두 다시 보낸 건, `skipped` = 상한·큐 가득·원본 미응답·GET 아님·허용 경로 밖. `match + mismatch = compared`.
- `target_5xx`는 대상 응답 5xx와 대상 연결 실패(표본의 `status_target`=0)를 함께 센다.

## 설계 결정

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| S1 | 복사 방식 | RDS에 허용 경로마다 `match.prefix` + `:method == GET` 헤더 라우트, `/`와 같은 동작(가중치·5초·연결 실패 재시도)에 `request_mirror_policies`(`cluster_shadow`, `runtime_fraction` percent/HUNDRED)만 더함. 관리·내부·실시간 경로 라우트 뒤, `/` 바로 앞 | 사용자 응답 경로는 그대로다. 복사본 응답은 Envoy가 버린다. 실시간 경로와 겹치면 실시간 라우트가 먼저라 복사하지 않는다 |
| S2 | 허용 목록 | 기본 거부. 빈 목록·`/`로 시작 안 함·막힌 경로(`/admin`·`/__cmp`)와 양방향 겹침(`/` 포함) 거절 | `/`는 모든 GET을 복사한다. 수집기도 GET·허용 경로를 다시 확인한다(이중) |
| S3 | `cluster_shadow` 위치 | bootstrap **정적** 클러스터(127.0.0.1:18090, STATIC, 헬스 체크 없음, 연결 제한 0.25s). `enable-shadow.sh`가 한 번 더하고 Envoy를 다시 띄운다 | CDS 파일은 relay-worker(`set_target`)가 `cluster_target` 하나로 통째로 덮어쓴다(74 R1과 같은 이유) |
| S4 | 켜고 끄기 | `EnvoyProxy.set_shadow`: 지금 라우트 파일의 가중치를 읽어 그대로 두고 복사만 바꿔 쓴다(원자 교체 + version_text/update_rejected 확인 재사용). 라우트 파일을 읽지 못하면 켜지 않는다. 꺼진 상태에서 끄기는 파일을 쓰지 않는다 | 가중치 Activity는 복사를 모른다 → 켜진 채 가중치를 바꾸면 복사가 꺼진다(안전한 쪽). 84는 DB 전환·트래픽 단계 전에 `shadow_stop`을 부른다 |
| S5 | 비교 | 원본 GET 2번 + 대상 GET 1번. 상태 코드, JSON은 키 정렬 후 JSON pointer 잎으로 펴서 비교, 지연(ms). 원본 2번 사이에 달라진 pointer는 잡음으로 배우고 **실행 전체에 소급**(요약 때 다시 계산) | 시각·요청 ID 같은 필드. JSON이 아니면 본문 sha256 하나(pointer `""`)로 비교한다 |
| S6 | 주소 | 실행을 켤 때 Envoy `/config_dump`에서 `cluster_origin`(정적)·`cluster_target`(동적 CDS) 주소를 읽는다. Host는 복사본의 `-shadow` 접미사를 뗀 값, 대상에는 `TRAFFIC_TARGET_HOST`가 있으면 그 값(Envoy와 같게) | 별도 설정을 두면 Envoy와 어긋날 수 있다 |
| S7 | 보관 | 본문은 보관하지 않는다(해시만). 불일치 표본은 쿼리 문자열을 빼고, 값은 형식·크기(`<string:5>`, `<number>`, `<bool>`, `<array:2>`, `<object:1>`, `<missing>`, `null`)로 가리고 **최대 20개**. 응답당 pointer 200개, 지연 10,000건, 실행 16개까지 | 개인정보·토큰이 요약·이벤트·Temporal 이력에 남지 않게 |
| S8 | 부하·안전 | 원본 재요청 토큰 버킷(초당 `rate_per_sec`), 큐 256(가득 차면 skipped), 작업 스레드 4, 다시 보내는 요청 제한시간 5초, 본문 2MB까지 읽음. 실행은 900초가 지나면 스스로 멈춘다. 연결·Envoy 내부·`x-forwarded-*`·`x-request-id` 헤더는 빼고 보낸다 | `shadow_stop`을 못 받아도 원본을 계속 두드리지 않는다 |
| S9 | 노출 | 복사본 수신 127.0.0.1:18090, 제어 API 127.0.0.1:18091. 루프백이 아니면 시작을 거부한다. 보안 그룹 변경 없음 | Envoy·relay-worker와 같은 호스트 |
| S10 | 실행 하나 | 새 `run_id`로 켜면 앞 실행은 끝난다. 같은 `run_id`를 다시 켜면 그대로(재시도 안전). `DELETE`는 멱등 | |

## 제어 API (127.0.0.1:18091)

```text
POST   /runs                 {run_id, migration_id?, allow_paths, percent, rate_per_sec} → 200 {ok, run_id} | 400 | 503(주소 못 읽음)
GET    /runs/{run_id}/summary → 200 요약(위 계약) | 404
DELETE /runs/{run_id}         → 200 {run_id, stopped: bool}   # 멱등
```

## 데모 앱 허용 경로 권장값
`kmsdevdata-sketch/demo-app` main `5b3d5b397ca1c5ebde2bde7da9712d9322399205`을 읽었다(scratch에 얕은 clone, 실행하지 않음). 모든 라우트는 `main.go:66-74` 한 `ServeMux`에 있다.

| 권장 | 경로(prefix) | 근거 | 비고 |
| --- | --- | --- | --- |
| ✅ | `/api/messages` | `main.go:69` `GET /api/messages` → `listMessages`(`main.go:174-187`) → `store.After`가 `SELECT … WHERE id > $1 ORDER BY id LIMIT $2`만(`store.go:140-147`) | 같은 경로의 `POST /api/messages`(`main.go:68`, INSERT)는 Envoy `:method == GET` 조건으로 복사되지 않는다(Envoy v1.32에서 확인). 하위 경로 없음 |
| ✅ | `/api/stats` | `main.go:72` → `stats`(`main.go:305-312`) → `store.Stats` GROUP BY count SELECT만(`store.go:198-203`) | 쓰기 중이면 원본·대상 조회 시점 차이로 count가 다를 수 있다(잡음 또는 불일치로 보일 수 있음) |
| ❌ | `/api/stream` | `main.go:70` SSE(`text/event-stream`, 장기 연결) | 이미 실시간 경로(70). 복사하지 않는다 |
| ❌ | `/api/verify` | `main.go:71` POST | GET이 아니다 |
| ❌ | `/healthz` | `main.go:67` | 부작용은 없지만 `app_env`·`db_node`가 배포마다 달라 비교 가치가 없다 |
| ❌ | `/`, `/api` | `main.go:74` 정적 파일 전체 / 실시간·POST 포함 | prefix가 너무 넓다(`/`는 거절된다) |

권장 입력: `"allow_paths": ["/api/messages", "/api/stats"]`. 모든 응답에 `X-Served-By`(배포마다 다름) 헤더가 붙지만 헤더는 비교하지 않는다. `/api/messages` 행의 `app_env`·`db_node` 값은 DB 행 값이라 복제가 따라잡았으면 같고, 지연이 있으면 배열 길이가 다를 수 있다.

## 공개 인터페이스

```python
# cmp_traffic.envoy
SHADOW_CLUSTER = "cluster_shadow"
ShadowMirror(allow_paths: tuple[str, ...], percent: int = 100)        # 잘못된 값 ValueError
build_route_config(weights, version, target_host=None, stream_prefixes=(), shadow=None)
route_shadow(route_doc) -> ShadowMirror | None
EnvoyProxy.set_shadow(shadow | None) -> bool                         # 파일을 바꿨으면 True
cluster_address(config_dump, name) -> (host, port) | None
# cmp_traffic.relay
add_shadow_cluster(bootstrap_yaml, port=18090) -> str                # 멱등
# cmp_traffic.shadow
RunSpec, ShadowRun, Collector, Reply, TokenBucket, flatten, mask, strip_shadow_host,
serve_mirror(collector, "127.0.0.1", 18090), serve_control(collector, "127.0.0.1", 18091), main()
# cmp_worker.activities.traffic_shadow
shadow_start, shadow_summary, shadow_stop   # relay 큐(cmp_worker.workers.relay.relay_activities)
```

## 수용 기준
- [x] `shadow=None`이면 라우트 출력이 이전과 바이트 단위로 같다(고정 문자열 시험)
- [x] 켜면 관리·내부·실시간 라우트와 `/`는 그대로, 허용 경로의 GET만 `request_mirror_policies`(cluster_shadow, percent)
- [x] 위험한 허용 목록(빈 목록·`/`·막힌 경로 겹침·상대 경로)과 percent 범위 밖 거절
- [x] 켜기·끄기가 가중치를 바꾸지 않고, 두 번 끄기는 파일을 다시 쓰지 않는다
- [x] 원본 2번·대상 1번, `-shadow` Host 제거, 잡음 학습(소급), JSON 아닌 본문은 해시 비교
- [x] 값 마스킹·쿼리 문자열 제거·표본 20개 상한, 원본 재요청 초당 상한, GET 아님·허용 밖은 다시 보내지 않음
- [x] 제어 API(시작·요약·멱등 중지), 루프백 외 바인딩 거부
- [x] Activity: 수집기 없으면 Envoy를 건드리지 않음, Envoy 적용 실패 시 수집기 실행 정리, 수집기가 죽어도 끄기는 Envoy를 끔
- [x] relay 큐 워커 등록
- [x] Envoy v1.32 컨테이너(로컬)에서 생성 bootstrap·RDS 로드, GET 3건 복사·POST/허용 밖 미복사·`/now` 잡음·`/v` 불일치 마스킹 확인(2026-11-08)
- [ ] 중계 서버 반영(`enable-shadow.sh`, Envoy 1회 재시작) — 승인 필요
- [ ] 데모 리허설에서 84 Workflow와 함께 실측

## 시험
`tests/traffic/test_traffic_shadow_route.py`(라우트 고정·복사 라우트·검증·켜기/끄기·bootstrap), `tests/traffic/test_traffic_shadow_collector.py`(Host·pointer·마스킹·비교·잡음·표본 상한·상한·제어 API·루프백), `tests/traffic/test_traffic_shadow_activities.py`(Activity·워커 등록). `make check`. HTTP 경로·DB 변경이 없어 `make openapi`·alembic 영향 없음.

로컬 Envoy 확인 방법: 가짜 원본(19001)·대상(19002)과 수집기를 호스트 루프백에 띄우고, `render_bootstrap` + `add_shadow_cluster`(시험에서만 주소를 `host.docker.internal`로 바꿈) + `build_route_config(shadow=…)`를 `envoyproxy/envoy:v1.32-latest`에 물려 GET·POST·허용 밖 경로를 보냈다.

## 반영 절차 (승인 뒤)
1. 서버 `/opt/cmp/lib`을 이 커밋으로 맞춘다(`sync-lib.sh`).
2. 이전·전환 중이 아닐 때 `sudo infra/relay/enable-shadow.sh` — 수집기 설치·`cluster_shadow` 추가·Envoy 1회 재시작(몇 초). 실패하면 자동으로 되돌린다.
3. relay-worker를 다시 띄워 새 Activity를 받게 한다(`systemctl restart relay-worker`).
4. 끄기: `sudo infra/relay/enable-shadow.sh off`(라우트에서 복사 제거, 수집기 정지, Envoy 재시작 없음). 되돌리기: `… revert`.

## 남은 일
- 중계 서버 반영(승인 필요), 이상헌님 공유.
- 84: DB 전환 승인·트래픽 단계 **전에** `shadow_stop`을 부른다(S4). `shadow_summary` 실패는 경고로만 다룬다.
