# Ticket 30 — Sandbox 보안 프로브

## 상태
DONE (구현 완료, PR 대기) — DB 접근 프로브는 자동 테스트와 **실제 k3d(`cmp-sandbox`) 확인까지 완료**(2026-11-06). 평문 DB 거절·운영용 경로 차단 프로브와 `security-posture` Skill은 대상이 없어 후속. Issue 번호는 사용자가 발행한 뒤 기록한다.

## 목적
승인된 통신 의도와 기본 차단 NetworkPolicy가 Sandbox에서 실제로 동작하는지 시험 Pod로 증명한다. 원본에서는 클러스터 안 모든 Pod가 DB에 닿았는데 대상에서는 앱만 닿는다는 것을 "표시"가 아니라 "시험 결과"로 보여 준다.

## 관련 요구사항
AV-023, AV-012, AV-013

## 선행 작업
Blocked by: 12, 13, 24, 28 (병합됨).

## 설계 원칙: 앱 전용 테스트를 묶음에 미리 넣지 않는다
어떤 앱에도 쓸 수 있어야 한다. 프로브는 앱 중립 템플릿이고 대상(리소스, 포트, 허용 출발지)은 요구사항의 `network_intent`에서만 받는다. 기대값은 사람이나 AI가 앱을 보고 쓰지 않고 그 의도에서만 나온다(27-B와 같은 원칙). 대상이 없으면 케이스를 만들지 않고 사유를 남긴다.

## 구현 (DB 접근 프로브)
- **묶음 모델:** `ProbeStep`(`NETWORK_PROBE`): 출발지 역할 `ALLOWED_CLIENT`(출발 리소스를 이름으로 지정)/`OUTSIDER`, 대상 리소스, 포트, 기대 `CONNECTS`/`BLOCKED`. 이 단계가 없는 기존 묶음은 바이트와 SHA-256이 그대로다(고정 해시 테스트).
- **러너:** 연결은 러너가 열지 않고 호출자가 준 구현(`probes`)을 쓴다. 기대와 다르면 `PROBE_MISMATCH expected=… actual=…`, 구현이 없거나 구현이 깨지면 `PROBE_UNAVAILABLE`로 실패한다(통과로 넘어가지 않음).
- **케이스 생성(`cmp_testsuite/security.py`, 순수 함수):** 요구사항의 DB마다, 그 DB로 가는 TCP 통신 의도의 출발 리소스는 `ALLOWED_CLIENT`/`CONNECTS`, 의도 밖 Pod는 `OUTSIDER`/`BLOCKED`. 포트는 의도의 값이다. 대상이 없는 템플릿은 사유 코드로 남긴다: `DB_SSL_REJECT`(`TARGET_DB_SSL_NOT_CONFIGURED`), `ADMIN_PATH_BLOCKED`(`ADMIN_PATH_NOT_CONFIRMED`), DB가 없거나(`NO_DATABASE_IN_REQUIREMENTS`) DB로 가는 의도가 없으면(`NO_INTENT_TO_DATABASE`).
- **Sandbox 구현(`SandboxService.network_probe`):** Namespace 안에 시험 Pod(Sandbox 프록시와 같은 고정 이미지, 비특권, 환경변수·서비스 어카운트 토큰 없음)를 만들어 TCP 접속을 시도하고 종료 상태로 판정한다. 허용 출발지는 출발 리소스의 인스턴스 라벨을 달아 의도의 정책이 그 리소스로 보게 한다. 외부 Pod는 라벨이 없어 Namespace의 기본 차단이 DB에 닿기 전에 막으므로, **DB 쪽 수신 규칙만 판정하도록** DB로 나가는 임시 egress 정책을 함께 만든다. Pod와 임시 정책은 어떤 종료 경로에서도 지운다.
- **설정:** `SandboxSettings.security_probes`(기본 꺼짐). 켜진 때만 표준 묶음에 프로브 케이스가 더해지고, 꺼져 있으면 지금과 똑같다. 케이스는 `READ_ONLY`이고 `covers=["network-policy"]`다.

## 작업 범위
- 포함: 위 구현, 테스트, runbook.
- 제외: 평문 DB 연결 거절 프로브(대상 DB에 SSL이 없음, D-18), 운영용 경로 외부 차단 프로브(사용자 확정 필요, 29-B), `security-posture` Skill(이번 프로브는 요구사항에서 결정적으로 만들어져 모델 판단이 필요 없음. 운영용 경로 후보 제안이 필요해질 때), 배포된 대상에서의 재실행, 비HTTP 우회 경로 탐지.

## 수용 기준
- [x] 프로브 단계가 묶음에 들어가고 왕복된다. 이 단계가 없는 기존 묶음의 SHA-256은 그대로다.
- [x] 러너가 기대와 다른 답에 `PROBE_MISMATCH`, 구현이 없거나 깨지면 `PROBE_UNAVAILABLE`로 실패한다.
- [x] 케이스가 `network_intent`에서만 만들어진다: DB로 가는 통신 의도마다 허용 출발지 `CONNECTS`, DB마다 의도 밖 Pod `BLOCKED`. 포트는 의도의 값이고 리소스 ID는 케이스 ID로 쓸 수 있게 바뀐다. 기대값에 사람이 쓴 값이 없다.
- [x] 대상이 없는 템플릿은 케이스를 만들지 않고 사유가 남는다.
- [x] Sandbox 구현은 시험 Pod와 임시 정책을 어떤 종료 경로(통과, 실패, 대기 시간 초과)에서도 지운다. 비특권이고 비밀 값이 없다.
- [x] `security_probes`가 꺼져 있으면 묶음이 지금과 같고, 켜면 같은 입력에서 같은 묶음과 같은 해시가 나온다.
- [x] 실제 k3d에서 허용 출발지는 연결되고 외부 Pod는 차단되며 시험 Pod가 남지 않는다. `network_probe`를 직접 부른 것과, 통신 의도에서 케이스를 만들어 묶음으로 고정하고 `run_suite`로 돌린 전체 흐름 모두 통과했다(`tests/integration/sandbox/test_k3d.py::test_security_probes_show_only_the_intents_sources_reach_the_database`, 2026-11-06, 약 90초, 실행 뒤 `cmp-sbx` Namespace 0개).

## 테스트 전략
- Test Seam: 묶음 모델·러너(가짜 probe 구현), 순수 케이스 생성, `SandboxService`(기록하는 `FakeRunner`), 입력 조립, 실제 k3d 1회. 잡는 것: 판정 코드, 케이스 구성, 정리, 하위 호환, 실제 CNI에서의 NetworkPolicy 동작. 못 잡는 것: NetworkPolicy를 적용하지 않는 CNI(그 클러스터에서는 의미가 없다).
- 실행: `uv run pytest tests/testsuite tests/sandbox/test_network_probe.py tests/analysis/test_sandbox_input.py -q`, 이후 `make check`. 실제: `CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d`

## 문서
spec AV-023, runbook(보안 프로브), tasks.md를 같은 PR에서 갱신한다.

## 결정 대기
- 평문 DB 거절 프로브는 DB SSL 공급 방식(D-18)이 정해진 뒤, 운영용 경로 프로브는 29-B의 사용자 확정 뒤에 추가한다.
