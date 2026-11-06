# Ticket 66 — Agent 터널이 앱 포트 배정 즉시 헬스 확인·보고

## 상태
IN_PROGRESS (로컬 완료, 리뷰·Agent 릴리스 `agent-v0.4.1` 승인 대기) — [Issue #283](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/283). 2026-11-07 데모 실행에서 발견했다. 15·17·25·52 후속. 플랫폼 쪽 인계 대기는 다른 세션(`64-handover-tunnel-wait`)이 따로 고친다.

번호 메모: 처음에는 65로 착수했으나 main에 `65-onprem-provision-events.md`(#281)가 먼저 들어와 66으로 바꿨다. 첫 커밋 메시지와 원격 브랜치 이름(`provisioning/feat/65-tunnel-immediate-report`)에는 65가 남아 있다.

## 배경 (2026-11-07 실측, 로컬 k3d, Agent `cmp-kms-k3s-01` v0.4.0)
- 롤아웃 `SUCCEEDED READY`(약 19:37Z) 뒤 tunnel 컨테이너는 `APP_PORT_NOT_ASSIGNED`를 보고했고, 다음 바뀐 보고(`code: null`, `app_health_ok: true`)는 19:38:41Z였다.
- 플랫폼은 READY 결과가 기록된 뒤에야 `GET .../tunnel/config`의 `exposed_ports`에 앱 포트를 넣는다(16 T5, `cmp_api.tunnels.exposed_ports`). tunnel 컨테이너는 15초(`tunnel.intervalSeconds`)마다 한 번 설정을 읽고 확인·보고했다.
- 그래서 포트 배정 뒤 첫 헬스 보고까지 최대 한 주기(+앱이 막 뜬 경우 또 한 주기)가 걸렸고, 그동안 ON_PREM 트래픽 인계가 막혔다.

## 관련 요구사항
PROV-009, PROV-019, PROV-020, PROV-021

## 선행 작업
Blocked by: 15, 17, 25, 52 (완료). 플랫폼 쪽 인계 대기 작업과 파일이 겹치지 않는다.

## 작업 범위
- 포함: tunnel 컨테이너 확인 주기(빠른 재확인 창), agent 컨테이너의 작업 완료 신호, Chart 공유 볼륨, 문서
- 제외: 플랫폼 API·OpenAPI 변경(필요 없음), `provisioning_result.py`·worker 인계 Activity(64), 중계 서버 코드, Agent 릴리스·서버 배포(승인 대기)

## 수용 기준 (Issue #283)
- [x] 포트가 배정되면 정기 주기(15초)를 기다리지 않고 수 초 안에 `app_health_ok=true`가 보고된다
- [x] 대상이 없는 동안 호출 횟수에 상한이 있다
- [x] 정상 상태에서는 평소 주기를 지킨다
- [x] 상태 만료(TTL 60초·handshake 180초, 25) 규칙과 충돌하지 않는다
- [ ] 반영: 새 Agent 릴리스 `agent-v0.4.1`(승인 대기)

## Test Seam
- `cmp_providers.agent_tunnel.run_tunnel(manager, wait=, interval=, log=, wake=)`: 실제 `TunnelManager` + 가짜 플랫폼(`FakeClient`)·가짜 링크·가짜 시계(`wait`가 시간을 앞당김)
- `cmp_providers.agent_runtime.AgentDaemon.wake`: 처리된 작업마다 불린다
- `read_wake`/`write_wake`: 실제 임시 파일
- Chart: 실제 `helm template`(`tests/test_onprem_agent_chart.py`)
- 놓치는 것: 실제 k3d·중계 서버에서의 지연(미검증 참조)

## 착수 결정 (2026-11-07)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| I1 방식 | **작업 완료 신호 + 빠른 재확인 창.** agent 컨테이너가 작업(롤아웃·정리·사전 점검)을 처리할 때마다 같은 Pod의 공유 볼륨 파일(`/var/run/cmp-wake/job`)에 새 토큰을 쓴다. tunnel 컨테이너는 대기 중 1초마다 그 파일을 읽어 토큰이 바뀌면 빠른 창을 연다 | "대상이 없는 동안 계속 2초 간격"만으로는 앱이 배정되기 전 몇 시간 동안 호출이 7배로 는다. 창을 여는 근거가 필요하고, 배정 직전 사건(롤아웃 완료)을 아는 쪽은 같은 Pod의 agent 컨테이너다. 플랫폼 응답 힌트는 API·OpenAPI 변경이 필요하고 READY 기록 시점(worker)을 API가 미리 알지 못한다 |
| I2 빠른 창 | 창이 열리는 경우: tunnel 시작, 새 신호, `exposed_ports` 변경(평소 주기에서 본 것 포함). 창 안(120초)에서 보고 `code`가 `null`이 아니면 다음 확인은 2초 뒤, `null`(정상)이면 평소 주기. 창이 끝나면 평소 주기 | READY 기록은 롤아웃 보고 뒤 worker가 결과를 읽어 저장할 때라 몇 초~수십 초 늦다. 120초면 그 지연과 앱 기동을 덮는다. 포트 변경으로 창을 열면 신호 없이 배정된 경우에도 앱이 뜨자마자 보고한다 |
| I3 호출 상한 | 확인 간격은 어떤 경우에도 2초보다 짧지 않다(신호가 연달아 와도). 창 하나당 추가 호출은 최대 60회(설정 읽기·상태 보고 쌍). 플랫폼 오류 백오프(1·2·4…60초)는 신호가 와도 줄이지 않는다 | 보고 API 과부하 방지. 대상이 없는 평상시는 15초 주기 그대로 |
| I4 만료 규칙 | 바꾸지 않는다. 평소 15초 보고는 TTL 60초(25 F2) 안에 4번, 빠른 창은 더 자주 보고할 뿐이다. handshake 180초 판정·재연결(17 W6)도 그대로 | 25 규칙은 "보고가 끊김"을 본다. 더 잦은 보고는 신선도를 해치지 않는다 |
| I5 신호 파일 | Chart의 `tunnel-wake` emptyDir(`medium: Memory`, `sizeLimit: 1Mi`)를 두 컨테이너에 `/var/run/cmp-wake`로 마운트한다. 내용은 `time_ns-랜덤` 토큰뿐(비밀 없음), 임시 파일에 써서 교체(atomic). 쓰기 실패는 `tunnel_wake_failed`만 남기고 agent 루프를 멈추지 않는다(터널은 평소 주기로 동작). `tunnel.enabled=false`면 볼륨·인자가 없다 | 읽기 전용 루트 파일시스템 유지. 노드 디스크에 남지 않는다. 이전 Chart로 되돌리면 인자가 빠져 예전처럼 동작한다 |
| I6 API | 플랫폼 API·OpenAPI 변경 없음 | 기존 `GET .../tunnel/config`·`POST .../tunnel/status`로 충분 |

## 예상 지연
신호 뒤 120초 안에 READY가 기록되면: READY 기록 → 다음 확인까지 최대 2초 + 헬스 확인(정상 앱 수 ms, 최대 5초 시간 제한) → 보고. 즉 **보통 2초 안**(이전 최대 15초, 앱이 늦게 뜨면 30초). 신호 없이 배정되면 평소 주기에서 포트를 본 뒤 앱이 뜨는 대로 2초 안.

## 구현 요약
- `cmp_providers.agent_tunnel.run_tunnel(..., wake=, fast_interval=2, fast_window=120, tick=1)`; `TunnelManager.ports`(마지막 설정의 노출 포트); CLI `--wake-file`
- `cmp_providers.agent_runtime.read_wake`/`write_wake`, `AgentDaemon.wake`(처리된 작업마다), CLI `--tunnel-wake-file`
- Chart `infra/helm/onprem-agent/templates/deployment.yaml`: `tunnel-wake` emptyDir와 두 인자(터널 켤 때만)

## 검증 기록 (2026-11-07)
- `tests/test_agent_tunnel.py` 신규 6개(신호 뒤 30초 늦은 배정도 3초 안 보고, 30분 대기 호출 상한, 연속 신호에도 2초 미만 간격 없음, 정상이면 15초 주기, 신호 없이 포트 변경 시 앱 정상화 3초 안 보고, 실제 파일 신호), `tests/test_agent_runtime.py` 신규 1개, `tests/test_onprem_agent_chart.py` 신규 2개·기존 1개 갱신
- 실시간 확인(실제 신호 파일·실제 `threading.Event.wait`·실제 시계, 가짜 플랫폼·링크): 신호 3초 뒤 포트 배정 → **1.02초 뒤** `code: null`·`app_health_ok: true` 보고. 신호 전 평소 주기 유지 확인
- `make check`(ruff lint/format, pytest 전체)

## 미검증
- 실제 k3d·중계 서버 실측(이미지 빌드·중계 컨테이너 준비가 필요한 17 smoke 규모)은 하지 않았다. `agent-v0.4.1` 설치 뒤 데모 리허설에서 `kubectl -n cmp-system logs deploy/cmp-onprem-agent -c tunnel`의 `code:null` 시각과 READY 기록 시각 차이로 확인한다
- 64와 함께 쓸 때의 전체 인계 시간

## 관련 문서
- [15](15-agent-runtime-install.md) · [16](16-tunnel-config-exchange.md) · [17](17-agent-wireguard-tunnel.md) · [25](25-tunnel-status-freshness.md) · [52](52-agent-job-lifecycle.md) · [설치 안내 §7](../../specs/provisioning/onprem-agent-install.md)
