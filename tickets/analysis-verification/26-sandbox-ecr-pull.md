# Ticket 26 — Sandbox의 비공개 ECR 이미지 pull

## 상태
DONE — Issue #197 (ENV-05·06과 한 PR). 구현 완료, 실제 ECR 토큰 확인은 승인 후

## 목적
실제 빌드 경로(티켓 25)는 이미지를 플랫폼 ECR에 올린다. 로컬 k3d의 Sandbox는 인증 없이 비공개 ECR 이미지를 받을 수 없어 `ImagePullBackOff`가 난다. 민서님의 공용 모듈 `cmp_providers.ecr_pull`(Provisioning 29)로 단기 토큰을 만들어 Sandbox 네임스페이스에 pull Secret을 둔다.

## 관련 요구사항
AV-010, AV-011, AV-020

## 선행 작업
Blocked by: 25, Provisioning 29(`ecr_pull`, 병합됨). 실제 토큰 발급은 AWS 호출이므로 계정·프로필 사용 승인이 있어야 하고, 이 티켓의 구현·테스트는 가짜 토큰 발급기와 가짜 `kubectl`로 한다.

## 작업 범위
- 포함
  - `SandboxService`에 pull Secret 적용: 앱 이미지 중 ECR 이미지가 있으면 `PlatformRegistry`(설정 `BUILD_ECR_REPOSITORY`에서 도출)가 허용하는 것만 받고, 발급한 토큰으로 `kubernetes.io/dockerconfigjson` Secret `cmp-ecr-pull`을 네임스페이스에 만들고 `default` ServiceAccount의 `imagePullSecrets`에 연결(민서님 GKE 쪽과 같은 방식, 차트 변경 없음)
  - 토큰 발급기는 주입한다(`EcrTokenMinter`). 기본은 `boto3_minter(BUILD_AWS_PROFILE)`. 토큰은 메모리에서 Secret으로 바로 적용하고 인자·이력·로그·결과에 남기지 않는다(`kubectl apply`는 표준 입력)
  - 갱신: 갱신 실행(`update_sandbox`)에서도 같은 Secret을 다시 적용한다(토큰 12시간, Sandbox TTL 2시간이라 만료 전 정리됨)
  - 실패는 코드로: `ECR_TOKEN_UNAVAILABLE`, `ECR_ACCOUNT_MISMATCH`, `IMAGE_REGISTRY_NOT_ALLOWED`는 재시도 없이 사람 검토로
  - 사전 점검과 runbook: 비공개 ECR 이미지를 쓰려면 필요한 프로필·설정, 공개 이미지(`PrebuiltImages`)는 Secret 없이 동작
- 제외: 실제 AWS 호출 확인(승인 후 1회), 운영 대상 클러스터의 pull(민서님, Provisioning 29), ECR 저장소·권한 생성(Provisioning 28)

## 수용 기준
- [x] ECR이 아닌 이미지(공개)만 있을 때는 Secret을 만들지 않고 이전 동작과 같다.
- [x] 플랫폼 ECR 이미지가 있으면 `cmp-ecr-pull` Secret이 생기고 `default` ServiceAccount가 그것을 참조한다.
- [x] 플랫폼 레지스트리나 허용 저장소 밖의 ECR 이미지는 `IMAGE_REGISTRY_NOT_ALLOWED`로 거절된다.
- [x] 토큰이 명령 인자, Workflow 입력·이력, 결과, 로그, 오류 메시지에 나타나지 않는다.
- [x] 갱신 실행에서도 Secret이 다시 적용된다.
- [x] 토큰 발급 실패는 재시도 없이 코드로 실패한다(AWS 오류 메시지는 나오지 않음).

## 테스트 전략
- 제안 Test Seam: 가짜 `EcrTokenMinter`와 기존 `FakeRunner`(kubectl 명령 기록)로 Secret 내용·ServiceAccount 패치·명령 인자 검사, 설정 조립(순수). 잡는 것: 허용 범위, 값 비노출, 공개 이미지 무변경. 못 잡는 것: 실제 ECR 토큰과 k3d의 실제 pull(승인 후 1회 수동 확인).
- 실행: `uv run pytest tests/sandbox -q`, `uv run ruff check .`

## 문서
tasks.md, runbook(비공개 ECR 이미지 사용법), 25 티켓의 "k3d가 받는 방법" 미해결 항목 해소를 갱신한다.
