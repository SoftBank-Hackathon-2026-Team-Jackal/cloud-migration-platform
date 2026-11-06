# Ticket 54 — 빌드 중 인스턴스 메타데이터 자격 노출 차단

## 상태
IN_PROGRESS (로컬 완료, 리뷰·서버 반영 승인 대기) — [Issue #189](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/189). 2026-11-06 실동작 점검 B-11에서 발견했다. 우선순위 P1(보안). 28 후속.

## 배경
- 데모 EC2는 IMDSv2 필수, hop limit 2다(21: worker 컨테이너가 Docker bridge 한 단계를 거쳐 인스턴스 역할을 쓴다).
- worker는 호스트 Docker 소켓으로 사용자 앱을 빌드한다(28 D6). 데몬 내장 BuildKit(`docker` 드라이버)의 `RUN`은 기본 bridge `docker0`에서 돈다. 그래서 사용자 Dockerfile의 `RUN curl http://169.254.169.254/...`가 IMDS 토큰을 받고 인스턴스 역할 자격을 꺼낼 수 있다.
- 인스턴스 역할 권한: State 버킷 `cmp/*` 읽기·쓰기, ECR push, 배포 버킷 `sources/`·`tests/`·`builds/` 쓰기 등(점검 B-11).
- 근거: `infra/demo/cloudformation.yaml`(MetadataOptions), `apps/worker/src/cmp_worker/image_build.py`(`docker buildx build`), 점검 보고서 B(`claude/prov-audit-b-permissions-ea62ed` 브랜치 `docs/engineering/provisioning-runtime-audit/b-permissions.md` B-11).

## 관련 요구사항
PROV-001, PROV-021 · [제품 정책 P-10](../../docs/product/policies.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 선행 작업
Blocked by: 28 (로컬 완료), B-01 (완료). 46(#181)과 `infra/demo/` 파일이 가깝지만 `compose.yaml`·`ensure-secrets.sh`는 고치지 않는다.

## 작업 범위
- 포함
  - 위험 재현: 로컬 가짜 IMDS로 "빌드 `RUN`이 메타데이터 주소에 닿는가"를 확인한다.
  - 차단: 호스트에서 `docker0` → `169.254.169.254` 트래픽을 버리는 규칙(`infra/demo/build-imds-guard.sh`)과 재부팅·Docker 재시작 뒤 재적용(systemd), `start.sh`에서 설치.
  - 빌드 모듈: `RUN` 네트워크를 `default`로 고정하고, 다른 builder 선택(`BUILDX_BUILDER`)을 지우고, 네트워크 entitlement를 주지 않는다.
  - probe Dockerfile(`infra/demo/imds-probe/Dockerfile`)과 스모크(`scripts/smoke_build_imds_guard.py`), 단위·구조 테스트, 문서.
- 제외: 인스턴스 역할 권한 축소(47), compose 분석·ENV 설정(45), Docker 소켓 위험 자체 해소(rootless BuildKit·CodeBuild 전환), 서버 반영(승인 대기).

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 위험 판정 | **실제 위험이다(로컬 재현)**. 내장 BuildKit의 `RUN`은 `docker0`(실측 `172.17.0.2/16`, 기본 경로 `172.17.0.1`)에 붙고, `--network default` 빌드에서 가짜 IMDSv2의 토큰 PUT이 `200`을 받았다 | 가짜 IMDS는 별도 network namespace에 두고 veth로 Docker 호스트 너머 한 단계에 연결했다. 응답 TTL을 2로 두어 EC2 hop limit 2를 흉내 냈다. 트래픽이 실제처럼 호스트 FORWARD 체인(`DOCKER-USER`)을 지난다. 실제 IMDS 실측은 데모 서버에서만 가능하다(승인 대기) |
| D2 차단 방식 | **호스트 iptables**: `DOCKER-USER` 맨 앞에 `-i docker0 -d 169.254.169.254/32 -j DROP`(주석 `cmp-build-imds-guard`) | Docker가 사용자 규칙용으로 남겨 두는 체인이고 Docker가 고치지 않는다. AWS ECS 문서가 컨테이너의 IMDS 차단에 쓰는 방식과 같다. compose 서비스(worker·api)는 자기 `br-*` bridge에 있어 인스턴스 역할을 그대로 쓴다(실측). 비용·일정: 스크립트 하나, 서버 반영은 다음 릴리스의 `start.sh`로 끝난다 |
| D2-a 기각: `--network none` | 빌드 `RUN`에서 네트워크 전체를 끊는다 | `pip install`·`npm ci` 같은 의존성 설치가 깨져 대부분의 사용자 앱 빌드가 실패한다 |
| D2-b 기각: 전용 네트워크 | `docker buildx build --network <전용 네트워크>` | 내장 BuildKit은 지원하지 않는다(실측: `network mode "..." not supported by buildkit`). `docker-container` builder를 전용 네트워크에 두는 방법은 그 네트워크에도 결국 iptables 규칙이 필요하고, builder 수명·캐시와 28 D6의 Digest 일치 검증을 다시 해야 한다 |
| D2-c 기각(후속): rootless BuildKit | 별도 BuildKit 서비스(TLS)로 빌드 | Docker 소켓 위험은 줄지만 IMDS 차단은 따로 필요하다(rootless의 `RUN`도 호스트를 거쳐 나간다). 서비스·인증서·worker 설정이 늘어 데모 일정에 비해 크다. README의 운영급 대안으로 남긴다 |
| D2-d 기각: hop limit 1 | IMDS 응답이 컨테이너까지 오지 못하게 한다 | worker 컨테이너도 인스턴스 역할을 잃는다(21 설계와 충돌) |
| D3 차단 범위 | IMDS `/32` 하나만. `169.254.169.253`(VPC DNS) 등 다른 링크 로컬 주소는 막지 않는다. IPv6 IMDS(`fd00:ec2::254`)는 템플릿에 `HttpProtocolIpv6: disabled`를 명시해 꺼 둔다(기본값과 같음) | 빌드의 DNS·시간 동기화를 깨지 않는다. 규칙이 IPv4뿐이라 IPv6 엔드포인트가 켜지지 않게 고정한다. `MetadataOptions` 변경은 무중단 갱신이다 |
| D4 영속·설치 | `build-imds-guard.sh install`이 스크립트를 `/usr/local/sbin/cmp-build-imds-guard`로 복사하고 `cmp-build-imds-guard.service`(oneshot, `After`·`BindsTo`·`WantedBy=docker.service`)를 켠 뒤 규칙을 확인한다. `start.sh`가 worker를 올리기 전에 실행하고, 규칙이 없으면 릴리스가 실패한다 | iptables 규칙은 재부팅 때 사라진다. Docker가 시작될 때마다 다시 넣는다. `cloudformation.yaml` UserData는 고치지 않는다 — UserData 변경은 인스턴스를 멈췄다 다시 켠다 |
| D5 빌드 모듈 | `docker buildx build`에 `--network default`를 명시하고, `--allow`(entitlement)를 주지 않고, Docker CLI 환경에서 `BUILDX_BUILDER`를 지운다. 임시 `DOCKER_CONFIG`(기존)와 함께 항상 데몬 자신의 `docker` 드라이버를 쓴다. 인자는 `_buildx_command()`·`_docker_env()`로 모아 스모크가 같은 값을 쓴다 | 다른 builder(`docker-container`·원격)를 고르면 `docker0`를 벗어나 규칙이 적용되지 않는다. `RUN --network=host`는 `network.host` entitlement가 없어 거부된다(실측). host 네트워크 `RUN`은 호스트에서 직접 나가므로 규칙을 우회하기 때문이다. `ImageBuilder` 공개 인터페이스는 그대로다 |
| D6 검증 seam | 단위 테스트는 빌드 명령·환경, 구조 테스트는 규칙·설치·`start.sh` 순서·probe·템플릿을 고정한다. 실제 차단은 스모크(가짜 IMDS)로 확인하고 일반 `make check`에는 넣지 않는다 | 스모크는 특권 helper 컨테이너와 네트워크 namespace 조작이 필요하다 |

## 수용 기준
- [x] 빌드 중 `169.254.169.254` 접근이 막힌다(테스트 Dockerfile로 확인). — 로컬 가짜 IMDS: 규칙 없이 `token_put=200`(빌드 실패), 규칙 적용 후 `token_put=000 role_list=000`(빌드 성공). 데모 서버 실측은 승인 대기

## Test Seam
- `tests/test_image_build.py`: 빌드 명령의 `--network default`, entitlement 없음, `BUILDX_BUILDER` 제거, `_buildx_command()` 공유.
- `tests/test_build_isolation.py`: 규칙 스펙(`docker0`·`/32`·`DOCKER-USER` 1번), systemd 재적용, `start.sh`에서 worker 전에 설치, probe가 상태 코드만 출력하고 차단일 때만 성공, `MetadataOptions`(IPv4·v2), 스모크가 모듈 명령을 쓰는지.
- `scripts/smoke_build_imds_guard.py --fake-imds`: 가짜 IMDS 재현 → 규칙 적용 → probe 차단, `RUN --network=host` 거부, 사용자 정의 bridge(worker)는 도달, 정리.

## 검증 기록 (2026-11-06, 로컬 arm64 맥 Docker Desktop 28.4.0, buildx v0.28)
| 명령·환경 | 결과 |
| --- | --- |
| `docker buildx build --network default`(RUN `ip addr; ip route`) | `eth0 172.17.0.2/16`, `default via 172.17.0.1` — `docker0`(`172.17.0.0/16`)에 붙음 |
| `docker buildx build --network <사용자 정의 네트워크>` | `network mode "..." not supported by buildkit` (D2-b) |
| `uv run pytest tests/test_build_isolation.py tests/test_image_build.py` (RED: 구현 전) | 4 failed(`start.sh` 설치·`HttpProtocolIpv6`·`--network`·`_buildx_command`) → 구현 후 통과 |
| `uv run python scripts/smoke_build_imds_guard.py --fake-imds --platform linux/arm64` | `PASS baseline without guard: RUN reaches IMDS: token_put=200 role_list=401` / `PASS guarded: RUN cannot reach IMDS: token_put=000 role_list=000` / `PASS RUN --network=host is refused: entitlement network.host not allowed` / `PASS user-defined bridge (worker) still reaches IMDS: 200` |
| 스모크 뒤 정리 확인 | `DOCKER-USER`의 `cmp-build` 규칙 0개, veth 없음, 가짜 IMDS 컨테이너·네트워크·probe 이미지 없음 |
| `make check` (ruff + pytest) | 2014 passed, 9 skipped |

`role_list=401`은 probe가 토큰 없이 조회하기 때문이다(토큰·자격 값은 probe가 읽지도 출력하지도 않는다). 응답이 왔다는 것 자체가 도달 근거다.

## 미검증
- 데모 서버(AL2023, Docker Engine·iptables-nft)에서 `build-imds-guard.sh install`의 systemd 설치와 실제 IMDS 차단: 서버 반영 승인 필요. 로컬 Docker Desktop VM에는 systemd가 없어 `install`은 돌리지 않았다(`apply`·`check`·`remove`만 실행).
- 재부팅 직후 Docker 시작과 unit 재적용 사이의 짧은 틈: 빌드는 worker 작업 때만 돌아 실제 노출 가능성은 낮다고 판단했다(실측 안 함).
- 빌드 `RUN`의 다른 외부 통신(인터넷, VPC 내부 주소)은 막지 않는다. Docker 소켓 위험(호스트 root와 같음)도 그대로다 — rootless BuildKit·CodeBuild 전환은 후속.

## 서버 반영 절차 (승인 대기)
1. 이 PR 병합 후 `bash infra/demo/release.sh <SHA>` — `start.sh`가 `sh infra/demo/build-imds-guard.sh install`을 실행한다(규칙 없으면 릴리스 실패).
2. SSM 세션: `iptables -S DOCKER-USER | grep cmp-build-imds-guard`, `systemctl is-enabled cmp-build-imds-guard.service`.
3. SSM 세션, 릴리스 디렉터리에서 `docker buildx build --network default --no-cache --progress plain infra/demo/imds-probe` → `imds_probe token_put=000 role_list=000`, 빌드 성공.
4. `docker exec cmp-demo-worker-1 aws sts get-caller-identity --query Arn --output text` → 인스턴스 역할(worker 영향 없음).
5. (선택) `cloudformation.yaml`의 `HttpProtocolIpv6: disabled` 반영: `bash infra/demo/deploy.sh` 변경 세트 검토 후. 기본값과 같아 급하지 않다.

## 문서 변경
- `infra/demo/README.md`: 빌드 격리(IMDS 차단) 보안 메모, 서버 점검 명령
- `infra/demo/cloudformation.yaml`: MetadataOptions 주석·`HttpProtocolIpv6`
- `docs/product/policies.md` P-10: 빌드 실행이 플랫폼 실행 자격에 닿지 않아야 한다는 문장
- `tickets/provisioning/28-*.md`·`b-01-*.md`: 후속 링크
- spec·plan·shared-contracts·OpenAPI: 외부 계약·경계 변경 없음(문서 변경 없음)

## 관련 문서
- [28](28-build-registry-runtime.md) · [B-01](b-01-image-build.md) · [21](21-demo-provisioning-runtime.md) · [`infra/demo/README.md`](../../infra/demo/README.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (데모 서버 실측 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — 외부 API·도메인 경계·계약 변경 없음. 빌드 모듈 공개 인터페이스(`ImageBuilder`) 불변
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `apps/worker/src/cmp_worker/image_build.py`: `BUILD_NETWORK`, `_buildx_command()`, `_docker_env()`(`BUILDX_BUILDER` 제거)
- `infra/demo/build-imds-guard.sh`(신규), `infra/demo/imds-probe/Dockerfile`(신규), `infra/demo/start.sh`(설치), `infra/demo/cloudformation.yaml`
- `scripts/smoke_build_imds_guard.py`(신규), `tests/test_build_isolation.py`(신규), `tests/test_image_build.py`
