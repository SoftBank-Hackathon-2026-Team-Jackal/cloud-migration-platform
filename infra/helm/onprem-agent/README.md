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
| `networkPreflight.image` / `allowedImages` | `""` / `[]` | Network Preflight 검사 이미지(Digest 고정) |
| `tunnel.enabled` / `mode` / `intervalSeconds` | `true` / `auto` / `15` | WireGuard 터널 컨테이너 (17) |

검증 규칙은 `values.schema.json`, 렌더 테스트는 `tests/test_onprem_agent_chart.py`에 있다.
