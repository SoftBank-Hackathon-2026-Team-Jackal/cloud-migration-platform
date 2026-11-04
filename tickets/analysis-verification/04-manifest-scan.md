# Ticket 04 — 매니페스트·클러스터 리소스 스캔

## 상태
DONE — PR #54 병합, Issue #50

## 목적
k8s 매니페스트에서 앱 실행 요구, 클라우드 의존 항목, 원본 DB 이미지, 변환 못 할 항목을 뽑는다.

## 관련 요구사항
AV-004

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: YAML 매니페스트·Helm 차트(`helm template` 후) 파싱, Deployment·Service·Ingress·ServiceAccount·PVC·StorageClass·ConfigMap·Secret 참조 추출, 클라우드 의존 감지, PostgreSQL Pod 이미지 Digest 기록, 변환 못 할 항목 목록
- 제외: 클러스터 직접 조회(06에서 같은 파서 재사용)

## 수용 기준
- [x] ALB Ingress 어노테이션, IRSA 어노테이션, EBS StorageClass가 클라우드 의존으로 감지된다.
- [x] CPU·메모리·replica·probe·포트가 근거 경로와 함께 나온다.
- [x] PostgreSQL StatefulSet/Deployment의 이미지가 Digest(태그면 조회 필요 표시)와 함께 분석 결과에 정보로 기록된다(요구사항에는 넣지 않음).
- [x] 사이드카·initContainer·CRD·모르는 어노테이션이 변환 못 할 항목으로 나오고, ACK 등 AWS 리소스 CR은 AWS 전용 서비스로 분류된다.
- [x] Secret은 이름·키만 읽고 `data` 값은 버린다.

## 테스트 전략
- 합의된 Test Seam: `scan_manifests(path)` 결과. 잡는 것: 탐지 규칙. 못 잡는 것: 클러스터에서만 보이는 실제 상태.
- 실행: 데모 앱 축소 매니페스트 fixture, `uv run pytest tests/analysis/test_manifests.py`

## 완료 시 확인할 동작
fixture 매니페스트로 클라우드 의존 목록, 실행 요구, 원본 DB 이미지가 나온다.

## 검증 증거
- `uv run pytest tests/analysis/test_manifests.py`: 13 통과. fixture 매니페스트(`tests/fixtures/analysis/manifests/`)로 ALB·IRSA·EBS 감지와 근거 경로, 워크로드 요구(CPU·메모리·replica·probe·포트), PostgreSQL 이미지 Digest·조회 필요 표시, 사이드카·initContainer·CRD·사용자 정의 리소스·모르는 어노테이션, ACK 리소스의 AWS 전용 서비스 분류, Secret 값 비노출, 워크로드 참조(ConfigMap·Secret·PVC), 파싱 실패 파일 보고, Helm 렌더 호출을 확인했다.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 415 통과, `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 구현: `packages/scanners/src/cmp_scanners/manifests.py`(`scan_manifests`, `render_helm_chart`). `pyyaml`을 런타임 의존성으로 승격했다(이전에는 dev 전용). `uv.lock`은 해당 항목만 추가했고 `--locked` 동기화가 통과한다.
- 문서 변경: spec·plan 변경 없음(AV-004 범위 안).

## 한계와 미검증
- `render_helm_chart`는 가짜 `helm` 실행 파일로 호출 인자와 실패 처리만 검증했다. 실제 `helm template` 렌더는 이 환경에서 검증하지 못했다.
- 02의 `scan_manifests` Activity는 mock 그대로다. 소스 작업 공간 경로를 넘기는 연결은 07 조립 단계에서 한다.
- fixture는 작성자가 가정한 구성이며 실제 데모 앱·EKS 매니페스트로 검증하지 않았다. 데모 앱 저장소(`demo-app`)에는 k8s 매니페스트가 없고 `docker-compose.yml`만 있어, 실제 EKS 매니페스트 확보 후 fixture를 보강한다.
- 클러스터 직접 조회는 범위 밖이다(06). 첫 번째 컨테이너를 앱으로 보고 나머지를 사이드카로 분류하는 단순 규칙이라, 앱이 여러 컨테이너로 구성된 경우 오분류할 수 있다.
- PostgreSQL 탐지는 이미지 이름에 `postgres`가 들어간 경우이며 `exporter`는 제외한다. 별칭 이미지는 놓칠 수 있다.
- 모르는 어노테이션은 허용 접두사 목록(`kubernetes.io/`, `helm.sh/` 등) 기준이라 목록 밖의 일반 어노테이션이 모두 보고된다.

## 보강 (Issue #75)
- 실제 원본 매니페스트(`demo-api`)로 돌려 보고 빠진 부분을 보강했다: NetworkPolicy egress(앱 → 앱, 포트)를 `network_rules`로, 파드 템플릿 라벨을 `Workload.labels`로, ConfigMap 키(`configmaps`)와 `env`·`envFrom`·`secretKeyRef`로 주입되는 변수 이름(`Workload.env_names`)을 수집한다. 값은 읽지 않는다. DNS·다른 네임스페이스·CIDR egress는 앱 간 통신 의도가 아니라 제외한다.
- `tests/fixtures/analysis/source-like/stack.yaml`(실제 파일이 아니라 같은 모양으로 새로 쓴 가짜 값 fixture)로 확인한다. 실제 파일 확인은 환경 변수 `CMP_REAL_SOURCE_YAML`로 경로를 줄 때만 도는 로컬 테스트다(저장소에는 넣지 않음).

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 원본 DB 이미지 기록 추가. v2 동기화로 요구사항이 아닌 `AnalysisReport` 정보로 변경(이미지 선택은 D-11).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
