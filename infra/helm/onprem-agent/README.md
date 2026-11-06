# onprem-agent Chart

Cloud Migration Platform 온프레미스 Agent를 사용자 K3s에 설치한다. 릴리스(`agent-vX.Y.Z`)는 `oci://ghcr.io/softbank-hackathon-2026-team-jackal/charts/onprem-agent`에 이미지 Digest를 고정해 게시된다. 설치·권한·폐기 절차는 [Agent 설치·운영](../../../specs/provisioning/onprem-agent-install.md)을 따른다.

## 설치

패키지가 공개면(기본) 로그인 없이:

```bash
helm install cmp-agent oci://ghcr.io/softbank-hackathon-2026-team-jackal/charts/onprem-agent --version <X.Y.Z> -n cmp-system --create-namespace --set agentId=<agent-id> --set platform.url=https://<platform> --set enrollmentToken=<token>
```

패키지가 비공개면 `read:packages` 토큰으로 `helm registry login ghcr.io` 한 뒤, 같은 Namespace에 `kubectl create secret docker-registry cmp-agent-ghcr --docker-server=ghcr.io ...`로 pull Secret을 만들고 `--set 'imagePullSecrets[0].name=cmp-agent-ghcr'`를 더한다(전체 명령은 설치 문서 §2). 익명 pull 가능 여부는 `uv run python scripts/check_agent_package_access.py --version <X.Y.Z>`로 확인한다.

## 주요 값

| 값 | 기본 | 설명 |
| --- | --- | --- |
| `agentId` | `""` (필수) | 등록 토큰 발급 때 쓴 Agent ID |
| `platform.url` / `platform.caBundle` | `""` (URL 필수) | HTTPS 플랫폼 주소, 사설 CA PEM |
| `enrollmentToken` | `""` | 첫 설치에만. `helm upgrade` 때는 비운다 |
| `image.repository` / `digest` / `tag` | 릴리스가 Digest 고정 | `tag`는 로컬 빌드용 |
| `imagePullSecrets` | `[]` | 비공개 레지스트리 pull Secret 이름 목록(`- name: cmp-agent-ghcr`). Agent Pod에만 적용. Chart는 Secret을 만들지 않는다 (42) |
| `networkPreflight.image` / `allowedImages` | 공식 `postgres` 16.15-alpine Digest / `[]` | Network Preflight 검사 이미지(Digest 고정). 기본값으로 바로 동작하며 `""`이면 끈다 (52) |
| `proxy.https` / `http` / `noProxy` | `""` / `""` / K3s 기본 CIDR·`.svc` | 플랫폼 호출용 HTTPS 프록시. 설정하면 두 컨테이너에 `HTTPS_PROXY`·`NO_PROXY`(대소문자 둘 다)를 넣는다 (52) |
| `tunnel.exposeKubeApi` | `false` | 켜면 플랫폼이 정한 `kube_api_port`(기본 6443)를 터널 IP에서 `kubernetes.default.svc:443`으로 전달한다(73, ADR-007). 플랫폼 `TUNNEL_EXPOSE_KUBE_API=true`도 필요. RBAC 추가 없음 |
| `tunnel.enabled` / `mode` / `intervalSeconds` | `true` / `auto` / `15` | WireGuard 터널 컨테이너 (17). 켜면 agent가 작업 완료 신호(`/var/run/cmp-wake`, 메모리 emptyDir)를 보내 터널이 2초 간격으로 즉시 재확인한다 (66) |

ClusterRole에는 `watch`가 없고, 정리 작업(`ENVIRONMENT_TEARDOWN`)을 위해 Namespace delete가 있다. 폐기된 Agent는 재시작 반복 없이 대기한다(52).

검증 규칙은 `values.schema.json`, 렌더 테스트는 `tests/test_onprem_agent_chart.py`에 있다.
