# Ticket 11 — Sandbox 구성·적용·정리

## 상태
IN_PROGRESS — 구현 완료(실제 k3d `cmp-sandbox`에서 검증), 리뷰 대기, Issue #93

## 목적
Sandbox 워커가 구체 환경 값을 정하고 공통 차트로 Sandbox를 띄운 뒤, 끝나면 반드시 정리한다.

## 관련 요구사항
AV-011, AV-016, AV-020

## 선행 작업
Blocked by: 02B, 07, Provisioning 공통 렌더 v2(#24, DONE)

## 작업 범위
- 포함: 로컬 k3d Sandbox(`cmp-sandbox`), `prepare_sandbox`(Namespace·Release 이름 규칙, capabilities에서 StorageClass, DB 비밀번호 Secret 무작위 생성, `.env`와 예외 목록으로 앱 env Secret 생성, 변수별 `env_bindings`), `SharedDeploymentRequestV2` 구성과 `render_shared_deployment_v2` 호출, DB 이미지는 D-11 결과(그 전까지 공식 `postgres:16` Digest 고정값), `helm upgrade --install --wait`, 준비 검사(Pod Ready·`SELECT version()`·SQL·헬스), Namespace 삭제 정리, 실행 ID·만료 라벨, 만료 정리 작업
- 제외: 제자리 갱신·데이터 초기화(11B), NetworkPolicy(13), 내부 Ingress(차트 미지원), GKE/EKS Sandbox

## 수용 기준
- [x] 첫 적용 후 DB·앱 Pod Ready, 요구 버전 확인, SQL 접속, 앱 헬스가 통과한다.
- [x] 앱 env Secret에 예외 목록의 제외 변수와 DB 연결 변수가 없고, DB 변수는 리소스 출력 참조로 연결된다.
- [x] `.env` 값과 DB 비밀번호가 로그·결과·Workflow 이력에 없다.
- [x] 같은 실행의 재시도에서 DB 비밀번호 Secret을 다시 만들지 않는다.
- [x] 통과·실패·취소 모두에서 Namespace와 보존 PVC까지 지워진다.

## 테스트 전략
- 합의된 Test Seam: 격리 k3d + Helm에서 `prepare_sandbox`·`apply_sandbox`·`teardown_sandbox` 결과와 클러스터 상태. 잡는 것: 실제 구성·적용·정리. 못 잡는 것: 클라우드 StorageClass·Ingress Controller 차이.
- 실행: `uv run pytest tests/integration/sandbox -m k3d`, `make check`

## 완료 시 확인할 동작
fixture 요구사항·이미지로 Sandbox가 뜨고 준비 검사 후 정리까지 끝난다.

## 환경 준비 (D-08 결정: 승철, 로컬 k3d)
이 Mac에 Colima(Docker 런타임), `k3d` 5.9, `kubectl`, `helm` 3.22를 설치하고 전용 클러스터를 만든다. 기본 kubeconfig와 현재 컨텍스트는 바꾸지 않고 minikube는 쓰지 않는다.
```bash
brew install colima docker k3d kubectl helm@3   # helm은 3 계열(CI는 v3.17.3)
colima start --cpu 4 --memory 6
k3d cluster create cmp-sandbox --kubeconfig-update-default=false --kubeconfig-switch-context=false
k3d kubeconfig write cmp-sandbox -o ~/.kube/cmp-sandbox.yaml
```
통합 테스트는 `CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d`로 켠다(변수가 없으면 건너뜀). `k3d-cmp-sandbox` 외의 컨텍스트는 코드가 거절한다.

## 검증 증거
- `uv run pytest tests/sandbox/test_deploy.py`: 13 통과(가짜 실행기와 실제 `helm template` 렌더). 네임스페이스·릴리스 이름 규칙(결정적, 63·53자 이하, 실행별 구분), `k3d-cmp-sandbox` 컨텍스트만 허용, 라벨(실행 ID·만료·관리자)이 붙은 네임스페이스와 DB 비밀번호·URL·앱 env Secret이 stdin으로만 생성되고 인자·계획·결과에 값이 없음, 앱 env Secret에는 요청한 이름만 담김(제외 변수는 복사하지 않음), 같은 실행의 재시도에서 DB 비밀번호 Secret을 다시 만들지 않고 비밀번호만 남았을 때 URL Secret을 그 비밀번호로 다시 만듦, 요청이 `SharedDeploymentRequestV2`를 통과하고 공통 차트로 렌더되며 `DATABASE_URL`→`<DB release>-url`·`DB_PASSWORD`→`<DB release>-auth`·`.env` 변수→`<app release>-env` 참조로 풀림, DB → 앱 순서의 `helm upgrade --install --wait`와 임시 values 파일 삭제, 준비 검사(버전·SQL·앱 헬스, 비밀번호는 파드가 자기 Secret으로 읽음)와 코드만 있는 오류, 네임스페이스 삭제와 `--wait`, 샌드박스가 아닌 네임스페이스(`default`, `kube-system`, `cmp-prod-demo`) 삭제 거절, 만료된 샌드박스만 삭제.
- **실제 `cmp-sandbox` k3d에서 검증**(`tests/integration/sandbox/test_k3d.py`, 2 통과, 약 60초): PostgreSQL 16(공식 이미지, Digest 고정)과 `podinfo`(공개 앱 이미지, Digest 고정)를 `local-path` StorageClass로 띄워 `helm --wait` 통과, `select version()`이 `PostgreSQL 16`, 서비스 프록시로 앱 `/healthz` 응답, 앱 env Secret의 키가 요청한 `FEATURE_X` 하나뿐(`AWS_REGION` 같은 제외 변수는 없음), URL Secret 존재, 같은 실행을 다시 `prepare`해도 DB 비밀번호 Secret의 uid가 그대로, 정리 후 네임스페이스와 PVC가 모두 사라지고 클러스터에 `cmp-sbx-*`가 남지 않음. 계획·배포·검사 결과에 `.env` 값이 없음.
- Helm을 설치한 뒤 전체 `pytest`가 937 통과, 3 건너뜀(k3d 2개와 로컬 전용 1개)으로 통과하고 `ruff check`·`ruff format --check`도 통과한다. 이전에 Helm이 없어 못 돌리던 Helm 의존 테스트도 이제 로컬에서 통과한다.
- 구현: `apps/worker/src/cmp_worker/sandbox_deploy.py`(`SandboxService`: `prepare_sandbox`·`apply_sandbox`·`check_sandbox`·`teardown_sandbox`·`cleanup_expired`, `SandboxCluster`). 민서님의 `CommandRunner`를 가져다 쓰며 수정하지 않았다. 문서 변경: pyproject에 `k3d` 마커만 추가.

## 한계와 미검증
- 02B의 Sandbox Activity(`prepare_sandbox`·`apply_sandbox`·`teardown_sandbox`)는 아직 mock이고 이 서비스를 Workflow에 연결하는 일(입력 전달, 취소 시 정리, 이력에 비밀이 없는지)은 14의 루프 통합에서 한다. 그래서 "통과·실패·취소 모두에서 정리"는 정리 함수와 통과 경로만 실제 클러스터에서 확인했고, 실패·취소 경로는 02B Workflow의 `finally`가 이 함수를 부르도록 연결된 뒤 확인해야 한다.
- 애플리케이션 이미지는 공개 `podinfo`로 시험했다. 데모 앱 이미지(Build 결과)와 실제 DB 연결(`DATABASE_URL`을 읽는 앱)은 확인하지 않았다. 앱이 Secret의 URL로 DB에 실제로 붙는지는 데모 앱이 필요하다.
- 만료 정리는 함수만 있고 주기 실행(스케줄)은 연결하지 않았다. `local-path` StorageClass 기준이라 클라우드 StorageClass·Ingress Controller 차이는 검증하지 못한다.
- DB 이미지는 D-11 결정 전이라 공식 `postgres:16` Digest 고정값을 쓴다. 호출 Worker 위치와 시간 상한(D-12)은 결정 전이다.

## 결정 대기
- D-08 Sandbox 기반 클러스터 준비 주체
- D-11 DB 이미지를 고르는 대상 구성 단계의 소유자

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 호출부 결정 방식(구체 값·Secret 생성은 Sandbox 워커)과 계약 v2 반영. 갱신·초기화는 11B로 분할. v2 동기화로 `render_shared_deployment_v2` 사용.
- 2026-11-05(D-16): 앱이 DB 연결을 단일 URL 변수로 받으면 `env_bindings`가 `connection_url_secret_ref` 출력을 가리킨다. Sandbox 워커는 접속 URL(`postgresql://사용자:비밀번호@호스트:포트/DB이름`, 비밀번호 URL 인코딩)을 만들어 Secret에 저장하고 앱에는 Secret 이름·키만 주입한다. 운영 배포와 같은 형식·키 이름을 쓴다(형식은 민서님과 확정). V2에는 반영됐다(#81). Sandbox 워커는 `cmp_providers.database_url`의 `connection_url_secret_name(db_release)`(Secret `<DB release>-url`), `connection_url(username=, password=, host=, port=5432, database=)`(URL 값), `URL_SECRET_KEY`(`"url"`)를 써서 운영과 같은 형식으로 만든다. URL에는 비밀번호가 들어가므로 Values·로그·이력에는 남기지 않고 Secret 참조만 쓴다. 비밀번호·사용자 Secret 이름이 `<DB release>-url`과 같으면 렌더가 `SECRET_NAME_CONFLICT`로 거절한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
