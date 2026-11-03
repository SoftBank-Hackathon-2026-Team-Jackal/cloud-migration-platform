# 격리 K3s Agent — Ticket 04

## 범위와 경계

Issue #15는 02/03의 **실제 Helm 렌더 산출물**로 격리 K3s에 PostgreSQL·테스트 앱을 배포하는 Agent 실행 경로다. 사용자가 승인한 k3d 테스트 환경·폴링 방식을 따른다. CKA 학습용 minikube는 대상이 아니다.

현재 폴링 전송 계층은 **신뢰할 수 있는 로컬 SQLite 작업 spool**이다. 제출자와 별도 Agent 프로세스가 같은 로컬 spool을 사용한다. 원격 HTTP 폴링/등록/인증·NAT 연결·운영 Agent 배포 패키지까지 구현했다고 주장하지 않는다. 해당 전송 계층은 후속 운영 통합에서 대체해야 한다. 실제 Kubernetes 호출·리소스 배포·상태 조회는 수행하며 시뮬레이션이 아니다. Production 승인/Temporal 실행은 연결하지 않는다.

## 계약과 실행

- `SandboxJob`: SANDBOX_TEST, 작업/Agent ID, kube-system Namespace UID로 식별한 클러스터, 테스트 Namespace 이름·UID, StorageClass, DB/App 리소스 이름, Manifest ArtifactRef, 테스트 앱 포트·헬스 경로.
- `JobStore`: 동일 작업 ID/동일 payload 재제출은 기존 상태를 반환한다. 같은 ID의 변경 payload는 거절한다. 트랜잭션으로 한 작업은 한 poller만 QUEUED→RUNNING으로 가져간다. 완료는 SUCCEEDED, 결과 불명은 UNKNOWN이다. RUNNING/UNKNOWN을 자동 재배포하지 않는다. 이 spool에는 Secret 원문을 넣지 않는다.
- `K3sTarget`: 전용 kubeconfig·명시 context를 모든 kubectl 호출에 넘긴다. `k3d-cmp-*` 테스트 context만 허용한다. kube-system UID, 테스트 Namespace UID 및 `cmp.dev/test-job` 소유 라벨을 검증한다. `cmp-test-*` Namespace만 허용한다.
- `SandboxAgent.submit/poll_once/reconnect`: 작업 제출·1회 폴링·저장 상태 조회/불명 상태 조정의 공개 경계. CLI는 `python -m cmp_providers.k3s_agent --help`를 참고한다. 주기/장기 실행 감독은 호출자가 담당한다.

Agent는 StorageClass 접근/Provisioner를 확인한 뒤 **두 Manifest 모두** 정확한 바이트 크기·Digest·대상 Namespace/리소스 종류·이름을 검증하고 DB부터 적용한다. Helm을 다시 실행하지 않고 고정된 JSON List 산출물을 `kubectl apply`한다. Helm Release 설치/업그레이드 재시도 상태 관리는 후속 운영 배포 범위다. Chart/Values→렌더→JSON List 변환은 테스트 제출자에서 수행하고, 저장된 최종 JSON 바이트로 ArtifactRef를 발급한다. 로컬 Artifact 저장은 Digest 파일명만 사용해 참조 ID의 경로 해석을 피한다.

DB rollout → App rollout → PVC Bound·observedGeneration/ready/updatedReplicas → Service 프록시 경유 테스트 앱 HTTP/SQL 결과를 검사한다. 실제 spec이 렌더 spec의 필드와 일치하는지도 확인하며 API 기본값 추가만 허용한다. 연결 결과가 불명확하면 오류 원문을 기록하지 않고 UNKNOWN/INSPECTION_REQUIRED로 남긴다. 재연결 시 RUNNING/UNKNOWN은 읽기 전용 검증으로 복구하고 다른 Manifest·미완료 상태를 조용히 다시 적용하지 않는다. 재접속 중인 다른 live worker와의 운영 lease/취소 조정은 구현하지 않았다. 이 테스트 runner는 단일 Agent 프로세스 운영을 전제로 하고, 원격 운영 연결 전에 별도 설계가 필요하다.

스풀과 산출물 디렉터리는 신뢰 경계다. 해시가 같다는 사실은 권한/출처의 증거가 아니며, 임의 업로드 Manifest를 이 경계에 직접 제출하면 안 된다. 운영에서는 승인된 Chart/산출물 출처·소유권과 제한된 Kubernetes RBAC가 필요하다.

## 실제 재현

전제: Docker, kubectl, Helm 3.17.3, k3d 5.8.3, `uv sync --extra dev --locked`. 기본 kubeconfig를 바꾸지 않는 옵션은 [k3d 공식 안내](https://k3d.io/v5.8.3/usage/kubeconfig/)를 따른다. 아래 이름/포트는 이 작업 전용이며 이미 존재하면 새 이름과 포트를 사용한다.

```sh
mkdir -p /tmp/cmp-k3s-test
k3d cluster create cmp-prov-test \
  --kubeconfig-update-default=false --kubeconfig-switch-context=false \
  --api-port 127.0.0.1:6551 --no-lb \
  --registry-create 'cmp-prov-reg:127.0.0.1:5501' \
  --k3s-arg '--disable=traefik@server:0' --wait
k3d kubeconfig get cmp-prov-test > /tmp/cmp-k3s-test/kubeconfig
chmod 600 /tmp/cmp-k3s-test/kubeconfig
docker build -t localhost:5501/cmp-probe:test tests/integration/k3s_app
docker push localhost:5501/cmp-probe:test
docker pull postgres:16-alpine
```

Push로 확인한 앱 Digest와 pull로 확인한 DB Digest를 다음 인수에 넣는다. Pod에서 앱 Registry는 `cmp-prov-reg:5000`으로 접근한다. 테스트 이미지의 실제 플랫폼에 맞춰 `--platform`을 선택한다.

```sh
uv run python scripts/smoke_k3s.py \
  --kubeconfig /tmp/cmp-k3s-test/kubeconfig --context k3d-cmp-prov-test \
  --state-dir /tmp/cmp-k3s-test/run \
  --database-image 'postgres@sha256:<DB_DIGEST>' \
  --application-image 'cmp-prov-reg:5000/cmp-probe@sha256:<APP_DIGEST>' \
  --platform linux/arm64
```

스크립트는 임의 비밀번호를 메모리에서 만들고 stdin으로 테스트 Secret에만 전달한다. 고유 Namespace를 생성하며, 성공/실패 후 자신이 소유한 Namespace를 삭제한다. PVC도 **폐기 가능한 테스트 데이터**이므로 Namespace 삭제로 정리한다. 운영 데이터 삭제 정책이 아니다. 기본 kubeconfig 전후 바이트 동일성을 검사하고 `state-dir/evidence.json`에 비밀값 없는 결과를 기록한다.

검증 후 이번 테스트 클러스터와 Registry만 삭제한다. k3d 삭제 작업도 별도 KUBECONFIG를 지정한다. Docker 전체 prune, minikube stop/delete, 기본 context 변경은 하지 않는다.

```sh
KUBECONFIG=/tmp/cmp-k3s-test/kubeconfig k3d cluster delete cmp-prov-test
k3d registry list
# 클러스터와 함께 Registry가 제거되지 않고 남아 있을 때만:
# k3d registry delete cmp-prov-reg
```

## 완료 증거와 제한

[실제 결과](../../docs/verification/provisioning-04-k3s.json): K3s v1.31.5-k3s1 / ARM64, local-path 1Gi PVC Bound, DB/App Ready, Service→앱→인증 SQL 쓰기/읽기, 동일 ID 재전달·새 Agent 프로세스 재연결, 완료 응답 유실 주입 후 재배포 없는 복구, Namespace 정리, 기본 kubeconfig 불변을 확인했다. 완료 응답 유실은 테스트 harness에서 spool의 완료 기록을 RUNNING으로 되돌려 주입했으며 실제 Agent 프로세스가 Kubernetes를 조회해 복구했다.

테스트 앱은 SQL 연결 시 테스트 테이블/행을 쓰고 읽는다. 이는 실제 Volume을 사용한 DB 쓰기/읽기 증거이며 **Node 장애·백업/복원·Pod 재시작 후 데이터 내구성 시험은 아니다**. 이 테스트의 JSON 헬스 응답 규칙을 임의 사용자 앱에 강제하지 않는다. 운영 앱의 헬스/기능 검증 어댑터는 후속 범위다. AWS/GCP CSI·네트워크·IAM 검증도 대체하지 않는다.

`make check`는 Agent 공개 경계 18개를 포함한 186개 테스트다. 실제 K3s smoke는 명시적 선택 실행이며 일반 CI에서 성공했다고 표시하지 않는다. FastAPI/Temporal 스캐폴드 변경은 없다.
