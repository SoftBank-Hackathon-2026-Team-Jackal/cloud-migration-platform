# Ticket 33 — 레드팀 실행기 격리와 강제 확인

## 상태
DRAFT — 사용자 검토 대기. Issue 미생성. [ADR-008](../../docs/decisions/ADR-008.md) R1 확정 후 착수.

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
ADR-008 갱신, spec AV-024 추가, plan §2.7·§6, runbook, tasks.md를 같은 PR에서 갱신한다.
