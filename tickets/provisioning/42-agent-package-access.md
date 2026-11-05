# Ticket 42 — Agent 이미지·Chart를 사용자 클러스터가 받을 수 있게 (GHCR 공개·imagePullSecrets)

## 상태
IN_PROGRESS (코드·문서 완료, GHCR 패키지 공개 전환 승인 대기) — [Issue #177](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/177). 우선순위 P0. 15·29 후속.

## 배경 (점검 C-01, 2026-11-06)
- `agent-v0.3.0` 릴리스는 성공했지만 GHCR 이미지 `cmp-onprem-agent`와 Chart `charts/onprem-agent`가 **비공개**다. 익명 토큰 요청은 `401 UNAUTHORIZED`, `manifests/0.3.0`은 403이다(이 티켓 착수 때 다시 확인).
- 그래서 설치 문서·릴리스 노트의 `helm install oci://ghcr.io/.../onprem-agent --version 0.3.0`이 사용자 클러스터에서 Chart pull부터 실패한다. Chart를 따로 받아도 이미지가 `ErrImagePull`이다.
- Chart에 `imagePullSecrets` 값이 없어 인증으로 우회할 수도 없었다.

## 관련 요구사항
PROV-009, PROV-016, PROV-021

## 선행 작업
Blocked by: 15(DONE). 29와는 파일만 이웃한다(29의 `cmp-ecr-pull`은 배포한 앱·DB Pod용).

## 범위
- 포함: Chart `imagePullSecrets` 값·스키마·Deployment 적용, Chart 테스트, 릴리스 노트의 두 설치 경로(공개·인증 pull), 릴리스 뒤 익명 pull 점검 단계, 익명 pull 점검 스크립트, 설치 문서·Chart README·README 갱신
- 제외: GHCR 패키지 공개 전환 자체(조직 설정, 소유자 승인), 기존 `agent-v0.3.0` Release 노트 수정, 새 태그 push, Agent 작업 수명·프록시(52), 롤아웃 복구(51)

## 수용 기준 (Issue #177)
- [ ] 로그인하지 않은 클러스터에서 릴리스 노트 명령 그대로 Agent가 설치된다 — **공개 전환 승인 대기**(아래 절차)
- [x] (또는) 문서화된 인증 절차로 설치된다 — 설치 문서 §2·릴리스 노트·Chart README에 `helm registry login` + `kubectl create secret docker-registry` + `imagePullSecrets` 경로. 격리 k3d의 인증 레지스트리로 Secret 없이는 pull 실패, Secret을 주면 pull 성공을 확인
- [x] Chart가 `imagePullSecrets`를 받는다

## Test Seam
- Chart: 실제 `helm template`(`tests/test_onprem_agent_chart.py`) — 기본값엔 필드 없음, 값을 주면 렌더된 모든 Pod 템플릿에 같은 목록, 스키마가 문자열·빈 이름·자격 증명 필드·객체를 거부
- 점검 스크립트: `scripts/check_agent_package_access.check(version, owner, fetch=...)`에 GHCR 익명 동작(공개 = 토큰 + manifest 200, 비공개 = 토큰 401/manifest 403)을 흉내 낸 `fetch`를 주입
- 릴리스 workflow: YAML을 읽어 노트에 두 경로와 점검 단계가 있는지 확인(노트 heredoc은 로컬 bash로 렌더해 눈으로 확인)
- 격리 k3d: htpasswd 인증 `registry:2` + 로컬 Agent 이미지로 kubelet의 실제 pull
- 놓치는 것: 실제 GHCR 인증·공개 상태의 Chart pull(`helm install oci://`), Actions에서의 workflow 실행

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| G1 값 형식 | `imagePullSecrets: []`, 항목은 Kubernetes 필드와 같은 `{name}` 객체. 이름은 DNS-1123 subdomain(최대 253자), 다른 키는 스키마가 거부한다 | Helm Chart 관례와 같아 `--set 'imagePullSecrets[0].name=...'`로 그대로 넘긴다. 문자열 목록 허용은 형식이 둘이 되어 뺐다 |
| G2 Secret은 사용자가 만든다 | Chart는 Secret 이름만 참조하고 레지스트리 사용자·토큰을 값으로 받지 않는다(Secret 생성 템플릿 없음) | 토큰이 Helm Release 기록(`sh.helm.release.v1.*`)과 `helm get values`에 남지 않게 한다. 사용자는 `kubectl create secret docker-registry`로 만든다 |
| G3 적용 범위 | Chart가 만드는 Pod 템플릿은 Deployment 하나(`agent`·`tunnel` 컨테이너가 같은 이미지)이므로 그 Pod spec에 붙인다. Agent가 실행 중 만드는 Network Preflight 검사 Pod, 배포한 앱·DB Pod에는 적용하지 않는다 | 검사 Pod는 대상 Namespace에 생기므로 `cmp-system`의 Secret을 참조할 수 없고, 검사 이미지는 운영자가 고르는 공개 이미지(`networkPreflight.image`)다. 앱 이미지는 29의 `cmp-ecr-pull`이 맡는다. ServiceAccount 수준 연결은 Agent 권한 범위를 넓히지 않으려고 쓰지 않았다 |
| G4 기본 경로 | 공개 패키지·익명 설치가 기본(15 H4 유지). 비공개 인증 pull은 공개 전환 전·사설 미러용 대안으로만 문서화 | 사용자 결정 H4. 사용자에게 GitHub 토큰을 요구하지 않는 것이 목표다 |
| G5 릴리스 점검 | workflow 마지막 단계에서 `scripts/check_agent_package_access.py`로 익명 토큰·manifest를 조회해, 비공개면 `::warning::`을 남기고 릴리스는 실패시키지 않는다 | 가시성은 조직 패키지 설정이라 workflow가 바꿀 수 없다(`GITHUB_TOKEN`에 권한 없음). 실패로 처리하면 첫 게시 때마다 빨간 릴리스가 된다. 이미 만들어진 Release·패키지는 남는다 |
| G6 점검 방식 | GHCR 익명 토큰(`/token?scope=repository:<owner>/<pkg>:pull`) → `GET /v2/<owner>/<pkg>/manifests/<version>`(OCI index·manifest Accept)만 쓴다. 표준 라이브러리 `urllib`, 자격 증명 없음. 결과 `PUBLIC`/`PRIVATE`/`NOT_FOUND`/`ERROR <status>`, 둘 다 `PUBLIC`이면 종료 코드 0 | kubelet·Helm이 익명으로 하는 요청과 같다. `gh api`는 토큰에 `read:packages`가 필요하고 익명 경로를 증명하지 못한다 |
| G7 공개 전환 절차 | 조직 소유자가 GitHub에서 Organization → Packages → `cmp-onprem-agent` → Package settings → Danger Zone "Change visibility" → Public, `charts/onprem-agent`도 같게 한다. 같은 화면 "Manage Actions access"/"Repository source"에 이 저장소가 연결돼 있는지(다음 릴리스의 push 권한) 확인한다. 조직 설정 "Package creation → Public" 허용이 꺼져 있으면 먼저 켠다. 그 뒤 `uv run python scripts/check_agent_package_access.py --version 0.3.0`이 둘 다 `PUBLIC`이어야 한다 | 저장소가 비공개여도 패키지만 공개할 수 있다(조직 정책이 허용할 때). 공개는 되돌리기 어려운 외부 공개라 승인 대기로 둔다 |

## 구현 위치
- Chart: `infra/helm/onprem-agent/values.yaml`·`values.schema.json`·`templates/deployment.yaml`, 새 `infra/helm/onprem-agent/README.md`
- 점검: `scripts/check_agent_package_access.py`
- 릴리스: `.github/workflows/agent-release.yml`(노트 두 경로, `Check anonymous pull` 단계)
- 문서: `specs/provisioning/onprem-agent-install.md` §2·검증, `README.md`, 15 H4 후속 메모, 29 I11 범위 메모
- SDD 계약(spec·plan·shared-contracts·OpenAPI): 변경 없음. 플랫폼 API·Agent 작업 계약·Agent 코드가 바뀌지 않고 설치 Chart 값만 늘었다

## 검증
- `uv run pytest tests/test_onprem_agent_chart.py` 34 passed(새 7: 기본값 무필드, 적용, 스키마 거부 5종은 하나의 parametrize, 점검 스크립트 2, 릴리스 노트 1). RED: 새 값이 스키마 `additional properties 'imagePullSecrets' not allowed`로 실패, 점검 테스트는 스크립트가 없어 실패
- `helm lint`(값 지정) 통과, `helm package` 통과
- 릴리스 노트 heredoc을 로컬 bash로 렌더: 공개 명령·인증 명령의 `$GHCR_TOKEN`이 문자 그대로 남고 버전·Chart 주소가 치환됨
- 실제 익명 조회(읽기 전용): `scripts/check_agent_package_access.py --version 0.3.0` → 두 패키지 `PRIVATE`, 종료 코드 1
- 격리 k3d(`cmp-p42`, 전용 kubeconfig, htpasswd 인증 `registry:2`): 결과는 아래 "k3d 검증"
- `make check` 1858 passed·8 skipped(ruff·format 포함). HTTP·DB·Temporal 변경 없음이라 `make openapi`·alembic·`make smoke-temporal`은 해당 없음

## k3d 검증
환경: Docker Desktop(arm64) 위 k3d `cmp-p42`(K3s 1노드, Traefik 끔, 전용 kubeconfig, 기본 kubeconfig 미변경). 같은 Docker 네트워크에 htpasswd 인증 `registry:2`(`cmp-p42-reg:5000`, 무작위 비밀번호, containerd mirror는 HTTP endpoint)를 두고 로컬 `cmp-onprem-agent:dev`를 push했다. 소스 Chart를 `image.repository=cmp-p42-reg:5000/cmp-onprem-agent`, `tunnel.enabled=false`로 설치했다(Pod가 이미지를 받는지만 본다, 플랫폼 주소는 가짜).

| 경우 | 결과 |
| --- | --- |
| `imagePullSecrets` 없음(`p42-anon`) | 컨테이너 `waiting ErrImagePull` — `authorization failed: no basic auth credentials` |
| `docker-registry` Secret `cmp-agent-ghcr` + `--set 'imagePullSecrets[0].name=cmp-agent-ghcr'`(`p42-auth`) | Deployment Pod spec `[{"name":"cmp-agent-ghcr"}]`, 이벤트 `Successfully pulled image ... 90248532 bytes`, 컨테이너 `running` |

정리: `k3d cluster delete cmp-p42`, 레지스트리 컨테이너·push한 태그·htpasswd·임시 Docker 설정 삭제, `k3d cluster list`에 `cmp-p42` 없음(다른 세션의 `cmp-p51`은 건드리지 않음). 로컬 Docker에 `registry:2`·`httpd:2.4-alpine` 이미지가 남아 있다.

## 미검증
- 공개 전환 뒤 로그인 없는 클러스터에서 릴리스 노트 명령 그대로 설치(전환 승인 뒤 위 스크립트 + 새 k3d에서 확인)
- 실제 GHCR에 대한 `helm registry login`·`docker-registry` Secret pull(토큰이 없어 로컬 인증 레지스트리로 대신)
- Actions에서 새 `Check anonymous pull` 단계 실행(다음 `agent-v*` 태그 때)

## 승인 대기
- GHCR 패키지 두 개 공개 전환(G7 절차)
- 기존 `agent-v0.3.0` GitHub Release 노트에 인증 경로 추가(선택, GitHub 변경)

## 완료 체크리스트
- [x] Chart 값·스키마·템플릿과 테스트
- [x] 릴리스 노트·설치 문서·README 두 경로
- [x] 익명 pull 점검 스크립트·릴리스 단계
- [x] 로컬 k3d 인증 pull
- [ ] GHCR 공개 전환과 익명 설치 확인(승인 뒤)
