# Provisioning 67: 데이터 터널 입구 방향 옵션

- Issue: #297 (이슈 제목의 번호 71과 별개로 사용자 지정 파일·브랜치·커밋 번호 67 사용)
- 상태: IN_REVIEW — 구현·로컬 검증 완료, 제품 실측 대기
- 요구사항: DM-011, ADR-007
- 의존: 데이터 10·11, Provisioning 69(PR #296 실측)
- 착수: 사용자 승인 범위·시험 경계(계약, 방향별 매니페스트, 가짜 클러스터 Activity 순서)를 따른다.

## 범위와 시험 경계

기본 target 입구를 보존하고 source 입구·대상 송신 CIDR·AWS LBC NLB·TCP 헬스 포트·keepalive 25를 추가한다.
계약 오류, 방향별 매니페스트, 주소 대기·handshake·키 재사용, Service 우선 정리 순서를 로컬에서 확인한다.
가짜 클러스터 시험은 실제 AWS LBC/NAT/DB 복제를 증명하지 않는다. 실제 클라우드 변경은 이번 범위 밖이다.
복제 단계·Workflow 실패 분기(#72)·Agent·중계·웹·DB 스키마는 변경하지 않는다.

## 입력 예시

`data_migration.tunnel`(아래 문서용 IP를 대상의 실제 NAT 송신 공인 IP로 바꾼다):

```json
{"ingress": "source", "target_egress_cidrs": ["203.0.113.7/32"]}
```

`data_migration.target_kubeconfig_ref`는 Worker가 접근할 수 있는 대상 K3s kubeconfig 참조다.
원본 `source.local_host`는 원본 DB Service 주소다. 양쪽 `peer_host`는 기존처럼 플랫폼이 정한다.
기존 `{"source_egress_cidrs": ["203.0.113.8/32"]}`는 `ingress="target"`으로 계속 동작한다.

## 정리 경계

명시적 정리 Activity를 추가한다. 복제·역방향 복제·PgBouncer가 터널을 더 이상 사용하지 않을 때 호출한다.
Workflow 완료·실패만으로 자동 삭제하지 않는다. 입구 Service 삭제 완료(LBC finalizer)를 먼저 기다리고,
이 migration의 양쪽 게이트웨이·키 Secret·peer Service·대상 DB 허용 정책만 삭제한다. 공유 Namespace는 보존한다.

## 검증

- TDD: 계약·방향별 매니페스트·생성/정리 Activity·Workflow 전달/timeout 테스트의 RED → GREEN 확인.
- `uv run pytest tests/data/test_data_tunnel.py -q`: **51 passed**. 기본 target 회귀, source DNS 접속, NLB 준비가 DNS보다 늦는 경우, 방향별 timeout, 키 재사용·비밀값 비노출, Service 우선 정리·실패 중단·재시도 확인.
- `make check`: **3222 passed, 10 skipped, 4 warnings**, ruff check/format 통과(700 files). 경고는 기존 Alembic `path_separator` deprecation이다.
- `make openapi`: 성공, 생성 파일 변경 없음(HTTP 경로 변경 없음).
- 시작 스크립트 `sh -n`, `git diff --check`: 통과.
- `make smoke-temporal`: 실행 중인 로컬 Temporal 서버가 없어 미실행. 양방향 Workflow는 테스트용 Temporal 환경에서 통과.
- 코드 검토: Workflow 수정은 터널 Activity 호출 시간에만 한정. 복제 로직·run() 실패 분기·Agent·중계·웹·DB 스키마 변경 없음. 키 생성/전달은 기존 stdin/Secret 경계를 유지한다.

## 남은 실측

제품 Activity로 EKS↔k3d 생성·NLB healthy 대기·NAT handshake·터널 DB 조회, cross-zone 활성화 후 Pod 없는 AZ 주소 접속, 전체 복제·전환·롤백, 정리 후 AWS NLB·대상 그룹·SG 잔존 여부를 별도 승인 뒤 확인한다. 이번 작업은 서버·AWS·GCP를 변경하지 않았다. PR은 `Refs #297`로 연결하며 실측 전 DONE으로 표시하지 않는다.
