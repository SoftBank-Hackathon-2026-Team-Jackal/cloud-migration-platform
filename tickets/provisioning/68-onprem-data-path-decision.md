# Ticket 68 — 온프레미스 데이터 이전 경로 2안(게이트웨이 입구 원본 쪽) 결정 기록

## 상태
IN_PROGRESS (문서 PR 리뷰 대기) — [Issue #288](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/288). 결정 문서는 [ADR-007](../../docs/decisions/ADR-007.md).

## 배경
결선 데모 대상이 맥 k3d(NAT 뒤, arm64)로 확정됐다. 데이터 이전은 흉내 없이 실제 PostgreSQL 논리 복제여야 한다. 사용자는 데이터 담당이 EKS ↔ GKE에서 검증한 방식과 최대한 같게 가기를 원해 2안을 골랐다(2026-11-07). 이 티켓은 그 결정을 문서로 남기는 0단계다.

## 관련 요구사항
DM-001, DM-011(데이터), MNP-001, PROV-021

## 선행 작업
Blocked by: 없음. 데이터 티켓 10·11(게이트웨이, 클라우드 검증)과 Provisioning 17(Agent 터널)을 근거로 쓴다.

## 작업 범위
- 포함: ADR-007, `specs/data-migration/spec.md` DM-011 문단, `plan.md`·`tasks.md` 후속 행, `docs/architecture/domain-boundaries.md` 데이터 경로 한 문장, 이 티켓, `specs/provisioning/tasks.md` 한 줄
- 제외: 코드·서버·AWS 변경, 다른 담당 코드(분석·샌드박스, 트래픽·중계, 데이터), MNP 명세 변경(단계 3·6에서 함께 정함)

## 수용 기준 (Issue #288)
- [x] ADR-007에 문제·확인된 사실·선택지·결정·결과/위험·후속 단계가 있다
- [x] DM-011이 2안을 기본으로 적고 1안을 대안으로 남긴다
- [x] 데이터 plan·tasks에 후속 작업이 있다
- [x] `make check` 통과
- [ ] PR 리뷰·병합(오케스트레이터)

## Test Seam
문서 변경만이라 새 시험이 없다. `make check`(ruff, pytest 전체)로 회귀만 확인한다.

## 착수 결정 (2026-11-07)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 경로 | **2안**: 데이터 게이트웨이 재사용, 입구(UDP NLB)를 원본 EKS에 두고 k3d 게이트웨이가 밖으로 접속 | 1안(Agent 터널 + 중계 서버 + VPC 피어링)은 원본 VPC `10.20.0.0/16`과 중계 서버 `172.31.0.0/16` 사이 피어링이 필요하고, 데이터가 플랫폼 중계 서버를 지난다. 2안은 EKS ↔ GKE 검증 코드를 그대로 쓴다 |
| D2 worker 접근 | worker는 Agent 터널 + 중계 서버를 거친 kubeconfig로 k3d API에 붙는다(`target_kubeconfig_ref`) | `handoff.py`가 K3s에 이 값을 요구한다. DM-001(`kubectl exec … psql`)을 바꾸지 않는다 |
| D3 권한 | kubeconfig는 권한을 좁힌 ServiceAccount, 이전 뒤 삭제 | 사용자 클러스터 cluster-admin을 플랫폼이 갖지 않는다 |
| D4 입구 제한 | 원본 NLB는 대상 공인 IP에서 오는 51820/UDP만 허용 | 공개 입구 노출을 최소화한다 |
| D5 문서 범위 | DM-011 문단과 표 행만 고친다 | 같은 파일을 티켓 67이 고칠 수 있어 충돌을 줄인다 |

## 후속 단계
| 단계 | 내용 | 담당·티켓 |
| --- | --- | --- |
| 1 | PoC: 원본 EKS UDP NLB ↔ 맥 k3d 게이트웨이로 실제 복제·전환 | Provisioning 69 |
| 2 | Agent가 k3d API 포트를 터널에 노출 | Provisioning |
| 3 | 게이트웨이 입구 방향 옵션(`tunnel.py`) | 티켓 67 |
| 4 | 중계 서버 TCP 전달(worker → k3d API) | 트래픽 담당 확인 |
| 5 | k3d kubeconfig용 ServiceAccount | Provisioning |
| 6 | 원본 쪽 수정 5개(compose `DATA_MIGRATION_AWS_PROFILE`, marker 표 비교 제외, cutover 이미지 빌드, 실패 시 슬롯 정리, 체크섬 UTC) | 데이터 담당 확인 |
| 7 | 웹 시작 입력 연결 | Provisioning |
| 8 | 웹 화면·report | Provisioning |

## 미검증
- EKS NLB UDP 전달, 가정용 NAT UDP 매핑 유지, arm64 게이트웨이 이미지(단계 1에서 확인)

## 관련 문서
- [ADR-007](../../docs/decisions/ADR-007.md) · [ADR-006](../../docs/decisions/ADR-006.md) · [데이터 명세](../../specs/data-migration/spec.md) · [MNP 명세](../../specs/migration-network-preflight/spec.md) · [17](17-agent-wireguard-tunnel.md)
