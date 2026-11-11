# Ticket 38 — 레드팀 실행 배선: 실행기 도구·모델 호출·Finding 판단을 Worker에 연결

## 상태
IN_REVIEW — Issue #355. 선행 [티켓 37](37-redteam-workflow-connection.md)(#352, PR #354 병합). 단위·서비스 시험과 `make check`(3714개 통과), 실제 k3d 시험 3개, `make smoke-temporal` 통과(로컬). 실제 Claude 호출 1회는 사용자가 확인했다.

## 목적
#352에서 Workflow에 이은 레드팀이 운영 Worker에서 실제로 돌도록, 실행기 Pod 안의 도구 실행·모델 호출·Finding 판단·설정 연결을 채운다.

## 관련 요구사항
AV-024, [ADR-008](../../docs/decisions/ADR-008.md) 결정 2·5·8·14, R7, [ADR-004](../../docs/decisions/ADR-004.md)

## 범위
- 포함
  - 격리 Pod의 실제 k3d 실측(`tests/integration/sandbox/test_redteam_k3d.py`)과 거기서 드러난 결함 수정
  - 고정 도구 `probe_get`(`cmp_worker.redteam_tools`): 상수 프로그램 + argv(고정 주소·포트, 검증된 경로)만 바뀌고 셸 없음
  - 모델 호출(`cmp_worker.redteam_brain`): 다음 요청 선택(`decide`), Finding 판단(`detect`), 재현 여부(`reproduces`). 기존 `llm_factory`와 ADR-004 비용 상한을 쓴다(역할 이름 `redteam`)
  - 배선(`cmp_worker.redteam_wiring`, `workers/sandbox.py`): 모델이 켜진 Worker에만 `RedTeamKit`을 넘긴다. `SandboxSettings.redteam*` → `SandboxInput.redteam`(`assemble_sandbox_input`)
- 제외: `ACTIVE` 강도 실행 시험, 승인 화면·API, 보안 시도 이벤트, 도구 확대(POST 등), 데모 앱·정답지 작성, 데모 리허설(발견율 측정), 실제 모델 호출

## 수용 기준
- [x] 격리 Pod가 k3d에서 만들어지고 앱에만 닿으며(DB·클러스터 API·메타데이터·인터넷·DNS 차단) 모든 종료 경로에서 정리된다
- [x] 느슨한 정책이 주입되면 실행기를 만들기 전에 `REDTEAM_ISOLATION_NOT_ENFORCED`로 멈춘다(실제 클러스터)
- [x] 실행기에서 고정 도구가 실제 앱(podinfo)을 읽는다(실제 클러스터)
- [x] 가짜 모델로 `decide`·`detect`·`reproduces`가 닫힌다. 모델이 말한 증거가 아니라 루프가 실제로 보낸 요청과 응답만 Finding에 쓰인다
- [x] 설정을 켜지 않으면 입력에 `redteam`이 없고, 모델이 꺼진 Worker는 키트가 없어 `REDTEAM_NOT_CONFIGURED`
- [x] 실제 모델 1회(사용자 확인): 가짜 데이터의 취약 데모 앱 (정답지는 Agent 입력에 쓰지 않음)

## Test Seam
- 순수·서비스: 가짜 클러스터(`test_redteam_tools.py`, `test_redteam_isolation.py`), 가짜 모델(`test_redteam_brain.py`), 배선(`test_redteam_wiring.py`, `tests/analysis/test_sandbox_input.py`)
- 실제: `CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox/test_redteam_k3d.py -m k3d` (모델·키 없음)
- 놓치는 것: 실제 모델의 도구 선택·판단 품질, `ACTIVE`, EKS·GKE에서의 NetworkPolicy 강제

## 실제 클러스터에서 드러난 결함 (티켓 33 시험이 가짜 클러스터만 썼다)
1. **headless DB Service**: DB 차트의 Service는 `clusterIP: None`이라 격리 확인이 주소를 못 찾아 항상 `NOT_ENFORCED`였다. → Endpoints의 Pod 주소를 쓴다
2. **실행기 준비 전 반환**: Pod를 만들자마자 실행기를 내줘서 `exec`가 "container not found"로 실패했다. → 컨테이너가 Ready가 될 때까지 기다린 뒤 내주고, 못 되면 `REDTEAM_EXECUTOR_NOT_READY`
3. **환경변수 가정**: 티켓 33은 "환경변수 없음"이라 했지만 Kubernetes는 `KUBERNETES_SERVICE_*`를 항상 넣는다. 그 주소는 차단이 확인되고 토큰은 없다. 앱의 환경·Sandbox Service 이름은 들어 있지 않음을 시험으로 고정했다

## 구현 요약
- `LoopResult.calls`: 루프가 보낸(또는 거절한) 호출을 관찰과 같은 순서로 남긴다. Finding의 엔드포인트·재현 요청·증거는 모델이 지목한 번호의 실제 호출에서 가져온다. 보내지 않은 요청 번호, 없는 심각도는 Finding이 되지 않는다
- 경로가 앱 밖을 가리키면 `REFUSED` 호출로 바꿔 허용 목록이 거절하고 관찰로 돌려준다(턴은 쓴다)
- 모델 호출이 실패하면(예: 비용 상한) 실행을 조용히 끝내고 결과의 `model_error`에 코드를 남긴다. 재확인에서는 예외로 올려 Finding을 열린 채로 둔다
- Sandbox Secret 값(`sandbox_secret_values`)은 Worker 메모리에서만 쓴다: 도구 결과 마스킹, Finding·증거의 비밀 검사
- `SandboxSettings.redteam`을 켜려면 동의 참조에 더해 경로 목록(`redteam_endpoints`)이 있어야 한다(`REDTEAM_ENDPOINTS_MISSING`). 경로 목록의 출처는 1차에서 데모 앱이 주는 목록이다

## 티켓과 달라진 점·알려진 한계
- 승인 장부는 아직 프로세스 메모리다(Worker 한 대 가정)
- 공격 한 번마다 모델 호출이 턴 수 + 1회(detect) 든다. 호출 수·토큰은 ADR-004 장부가 센다
- `reproduces`는 모델 판단이다. 응답이 단순할 때는 규칙 기반 비교가 더 안정적일 수 있어 실제 호출 결과를 보고 정한다
- 요구사항 텍스트(마스킹)와 경로 목록만 모델에 준다. 소스·`.env`·정답지는 주지 않는다

## 문서
spec AV-024, runbook, tasks.md, 티켓 33·37을 같은 PR에서 갱신했다. 제품 정책·API 계약·책임 경계는 바뀌지 않았다.

## 검증 (2026-11-07, macOS)
- `make check`: 3714 passed, 35 skipped
- k3d(`cmp-sandbox`) 3개: 격리, 느슨한 정책 거절, 도구 실행. 시험이 만든 Namespace는 남지 않음을 확인
- `make smoke-temporal` 통과
- 미검증: 취약 데모 앱 발견율, `ACTIVE` (실제 Claude 호출은 확인됨)
