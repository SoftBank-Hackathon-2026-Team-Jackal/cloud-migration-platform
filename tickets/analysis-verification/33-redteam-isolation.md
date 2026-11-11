# Ticket 33 — 레드팀 실행기 격리와 강제 확인

## 상태
IN_REVIEW — Issue #332. 단위·서비스 시험과 lint 통과(로컬). 격리 k3d 실측은 사용자가 확인했다. 전체 pytest는 CI에서 확인 대기. [ADR-008](../../docs/decisions/ADR-008.md) R1~R8 확정.

## 목적
Sandbox Namespace에 레드팀 실행기 Pod를 띄울 수 있게 하되, **공격 코드 없이 격리만** 만들고 증명한다. 이 티켓은 배포 구성 변경이 중심이다.

## 관련 요구사항
AV-023(확장), AV-012, AV-013, 신규 AV-024(레드팀, 명세 반영은 이 티켓)

## 선행 작업
Blocked by: 30(병합됨), 26 Provisioning(병합 상태 확인). 결정: ADR-008 R1.

## 범위
- 포함
  - 레드팀 Pod 매니페스트: 비특권, capability 전부 제거, `readOnlyRootFilesystem`, 서비스 어카운트 토큰 없음, 환경변수·Secret 없음, digest 고정 이미지, CPU·메모리·`activeDeadlineSeconds`
  - Namespace Pod Security `restricted`, ResourceQuota, 동시 실행 상한
  - NetworkPolicy: 레드팀 Pod 전용 egress(앱 Service 포트 하나, 앱 주소는 IP 주입), 공통 `allow_dns`가 이 Pod에 적용되지 않게 선택자 분리, `cmp_providers.network_policy`의 변환 재사용
  - 강제 확인 `verify_isolation`: 금지 대상(다른 Namespace Pod, 메타데이터 IP, 클러스터 API, DB, 프록시)에 닿지 않음을 시험. 하나라도 닿거나 판정 불가면 `REDTEAM_ISOLATION_NOT_ENFORCED`
  - 대상 한정: 클러스터 allowlist + `cmp.sandbox/run-id` 라벨 확인(`REDTEAM_TARGET_NOT_ALLOWED`)
  - 옵트인 설정과 동의 기록 필드(기본 꺼짐), Job 이름 결정성, 모든 종료 경로 정리
- 제외: 보안 도구 실행(34), 마스킹·초기화(34), LLM(35), 수정 연계(36), Production 적용

## 수용 기준
- [ ] 옵트인이 꺼져 있으면 Namespace·묶음·해시가 지금과 같다(회귀 없음, 고정 해시 테스트)
- [ ] 격리 확인 시험이 허용 경로(앱 포트)는 통과, 금지 대상 6종은 모두 차단으로 판정한다
- [ ] 정책을 일부러 느슨하게 만든 클러스터에서 `REDTEAM_ISOLATION_NOT_ENFORCED`로 멈추고 어떤 공격도 하지 않는다
- [ ] 레드팀 Pod에는 DNS 질의도 나가지 않는다. 다른 앱 Service 이름을 조회해도 응답이 없다(이름 노출·DNS 유출 통로 차단). `allow_dns`는 `podSelector: {}`로 전체 Pod에 적용되므로 레드팀 역할 라벨을 제외하는 선택 인자를 추가하되, 기존 Sandbox·운영 출력은 바이트 단위로 같다
- [ ] 레드팀 Pod의 라벨은 앱·DB·프록시·Agent의 기존 선택자(`app.kubernetes.io/instance` 등)와 겹치지 않는다. 라벨이 겹치면 다른 Pod의 허용 규칙(프록시의 외부 egress 등)을 얻을 수 있으므로 매니페스트 검사로 고정한다
- [ ] 레드팀 Pod에 허용 규칙으로 선택되는 egress가 앱 Service 하나뿐임을 NetworkPolicy 평가기로 증명한다(다른 앱·다른 Namespace·사설 대역·인터넷·메타데이터·클러스터 API 전부 차단)
- [ ] 해당 Namespace에 hostNetwork·hostPort·특권 Pod가 만들어지지 않는다(Pod Security `restricted` 적용 확인, NetworkPolicy는 hostNetwork Pod에 적용되지 않음)
- [ ] 라벨이 없거나 allowlist 밖 Namespace·클러스터에서는 Pod를 만들지 않는다
- [ ] 통과·실패·시간 초과·워커 재시작 어느 경로에서도 Pod와 임시 정책이 남지 않고, 재시도에서 Pod가 중복되지 않는다
- [ ] 매니페스트에 Secret·환경변수·토큰이 없음을 검사로 고정한다

## Test Seam
- 순수: 매니페스트·정책 생성(NetworkPolicy 평가기로 허용·차단 판정, 매니페스트 속성 검사)
- 서비스: 기록하는 `FakeRunner`로 순서·정리·결정적 이름·`NOT_ENFORCED` 분기
- 실제: 격리 k3d(`cmp-sandbox`)에서 허용/차단 경로, 느슨한 정책 주입 시 중단 확인. 실행: `uv run pytest tests/integration/sandbox -m k3d -k redteam`
- 놓치는 것: EKS·GKE Sandbox의 CNI 차이(별도 확인), 컨테이너 탈출

## 문서
ADR-008 갱신, spec AV-024 추가, runbook, tasks.md를 같은 PR에서 갱신했다. plan.md는 바꾸지 않았다(§2.7의 규칙은 그대로이고 레드팀 정책은 AV-024에 둔다. 마스킹·외부 차단을 다루는 §6은 티켓 34에서 갱신).

## 구현 요약
- `cmp_providers.network_policy.allow_dns(..., exclude_label=)`, `base_policies(..., dns_exclude_label=)`: 인자를 주지 않으면 기존 출력과 같다.
- `cmp_worker.sandbox_redteam`(순수): 역할 라벨, `egress_policy`, 실행기·확인 Pod 매니페스트, `restricted_violations`(Pod 보안 검사), 확인 스크립트와 판정(`isolation_verdict`).
- `cmp_worker.sandbox_network.build_manifests(..., redteam=False)`: 켜면 DNS 규칙에서 실행기를 빼고 `cmp-redteam-egress`를 더한다.
- `cmp_worker.sandbox_isolation.RedTeamIsolation.session`: 대상 확인 → 동시 실행 확인 → 정책 적용 → 확인 Pod로 증명 → 실행기 생성 → 정리(Pod·정책 삭제, 원래 DNS 규칙 복원).
- `SandboxSettings.redteam`, `redteam_consent_ref`: 아직 소비하는 곳이 없다(실행 시작은 티켓 34의 승인).

## 티켓과 달라진 점
- **Namespace 전체 Pod Security `enforce: restricted`와 ResourceQuota는 넣지 않았다.** 앱·DB 차트가 그 기준을 전제로 만들어지지 않았고, 데이터 초기화가 앱을 다시 띄우므로 재시작이 막힐 수 있다. 대신 실행기·확인 Pod를 만들기 전에 `restricted_violations`로 검사하고(hostNetwork·특권·hostPort·capability·토큰·환경변수 등), 동시 실행은 클러스터 전체 1개로 제한했다.
- Job이 아니라 Pod다(ticket 30의 시험 Pod와 같은 방식, 종료 시한 `activeDeadlineSeconds`).
- 클러스터 allowlist는 기존 `SandboxCluster`의 컨텍스트 검사(`CONTEXT_NOT_ALLOWED`)가 이미 강제한다. 새로 더한 것은 Namespace의 실행 ID·관리 라벨 확인이다.

## 검증 (2026-11-07, Windows)
- `tests/sandbox/test_redteam_isolation.py` 36개 통과(매니페스트·정책을 NetworkPolicy 평가기로 판정, 기록하는 `FakeRunner`로 세션 순서·정리·실패 경로). `ruff check`·`ruff format --check` 통과.
- 이 PC에서는 전체 pytest가 `fcntl` 없음과 `cp949` 인코딩으로 일부 파일을 불러오지 못한다(기존 코드, 이 변경과 무관). 전체 회귀는 CI(`check.yml`, Ubuntu)로 확인한다.

## 실제 k3d 실측 (티켓 38)
실제 `cmp-sandbox`에서 처음 돌려 결함 3개를 찾아 고쳤다(headless DB Service 주소, 실행기 준비 전 반환, 환경변수 가정). 자세한 내용과 시험은 [티켓 38](38-redteam-execution-wiring.md). 이 시험으로 허용 경로(앱)와 차단 경로(DB·클러스터 API·메타데이터·인터넷·DNS), 느슨한 정책 주입 시 중단, 정리까지 확인했다. EKS·GKE는 확인하지 않았다.

## 미검증
- 실제 k3d에서의 허용·차단 경로와 느슨한 정책 주입 시 중단(`tests/integration/sandbox -m k3d -k redteam`는 아직 없다). 이 PC에 Docker가 없어 만들지 못했다.
- EKS·GKE Sandbox의 CNI 강제.
- 확인 Pod의 실제 연결 판정 스크립트(`VERIFY_SCRIPT`)는 실제 클러스터에서 돌려 보지 않았다.
