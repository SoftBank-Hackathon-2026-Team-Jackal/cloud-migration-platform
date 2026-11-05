# Helm charts

`postgres/`는 Sandbox/Production 및 AWS/GCP/ON_PREM에서 공유하는 PostgreSQL 16 DB 전용 Chart다. App Chart는 별도 `application/` 디렉터리와 Release를 사용한다.

## 렌더 검증

Helm **3.17.3**을 PATH에 설치한다(CI도 동일 버전). Python 테스트는 `HELM_BIN`으로 다른 설치 경로를 지정할 수 있으며 Helm이 없으면 검사를 건너뛰지 않고 실패한다.

```sh
helm lint infra/helm/postgres -f tests/fixtures/contracts/postgres-values.json
helm template sandbox-postgres infra/helm/postgres \
  --namespace sandbox-001 -f tests/fixtures/contracts/postgres-values.json
uv run pytest tests/test_postgres_helm.py
```

예제 이미지 Digest·StorageClass·Secret은 렌더용 테스트 데이터다. 실배포 가능한 이미지/리소스를 보장하지 않는다. 실제 입력은 `cmp_contracts.chart.PostgresChartValues` 구조이며 `values.schema.json`은 그 모델에서 생성한다. 스키마 일치 회귀 검사가 계약 변경 누락을 감지한다. 기본 Values는 비어 있으며 모든 필수 입력을 제공해야 한다. CLI Release/Namespace도 Values와 일치해야 한다. 선택 필드 `database.parameters`(27)가 있으면 컨테이너 `args`에 `-c wal_level=… -c max_replication_slots=… -c max_wal_senders=…`와 `-c app.<키>=…`(키 정렬)를 넣는다. 허용 목록 밖의 설정은 스키마가 거절하고, 필드가 없으면 `args`가 없다.

- 단일 PostgreSQL StatefulSet, 내부 headless Service(5432), 명시 StorageClass의 ReadWriteOnce PVC를 만든다. Service DNS는 `<release>.<namespace>.svc`다.
- CPU millicores와 메모리 MiB는 requests/limits에 동일 적용하고, 디스크는 별도 PVC GiB로 적용한다. 지원 이미지 플랫폼에 따라 Linux/CPU node affinity를 설정한다.
- 기존 Secret의 이름/키만 `secretKeyRef`로 연결한다. Secret 생성·비밀번호·클라우드 Credential 입력은 없다. Namespace/StorageClass/Secret 준비와 존재·권한 검사는 배포 담당자의 책임이다.
- 이미지는 Digest로 고정하고 공식 PostgreSQL 16 이미지의 `POSTGRES_*` 및 PGDATA 인터페이스를 지원해야 한다. 버전 필드나 Digest 형식만으로 실제 이미지의 버전/실행을 검증할 수 없다. `registry_auth_ref`가 있는 입력은 이 티켓에서 아직 대상 imagePullSecret으로 해석할 수 없으므로 명시적으로 거절한다. 이미 인증된 노드 또는 공개 Registry 경로만 렌더 가능하며 비공개 Registry 연결은 후속 배포 작업이다.
- PGDATA는 볼륨 아래 하위 디렉터리를 사용한다. readiness는 `pg_isready`이며 인증된 SQL 접속 검증을 대신하지 않는다. DB 이름/사용자/비밀번호는 신규 데이터 디렉터리 초기화 시 사용되고 기존 DB의 변경/회전을 수행하지 않는다.
- PVC에 `helm.sh/resource-policy: keep`을 설정해 Helm 삭제 대상에서 보존한다. Chart는 자동 삭제/rollback/재설치를 하지 않는다. ON_PREM Agent는 정착하지 않은 업그레이드를 직전 Revision으로 `helm rollback`하며 PVC는 그대로 둔다(51). 보존 PVC의 재사용·확장·StorageClass 변경은 실제 상태와 데이터 소유권을 검증한 후 별도로 결정해야 한다.

현재 결과는 로컬 Manifest다. S3 저장·`RenderedChart` 참조 발급, Helm 설치, 실제 PVC Bound/마운트, DB 접속, 데이터 복사/CDC는 구현 범위 밖이다. 이후 저장 경계는 이 Chart/Values/Manifest의 정확한 바이트로 공통 ArtifactRef를 발급해야 한다.

## Application Chart

`application/`은 앱 Deployment·ClusterIP Service를 렌더한다. [입력·바인딩·공개 렌더 함수](../../specs/provisioning/application-rendering.md)를 따른다. `tests/fixtures/contracts/app-values.json`은 DB 동적 슬롯이 null인 계획 템플릿이므로 DB 연결 결과를 바인딩한 뒤 렌더한다. 원문 환경변수는 받지 않고 ConfigMap/Secret 이름·키 참조를 사용한다. 실제 ConfigMap/Secret 생성과 클러스터 배포는 별도 실행 경계다.

같은 App Chart는 [공통 렌더 계약 v2](../../specs/provisioning/onprem-shared-deployment.md#v2-공통-렌더-입력반환)의 `ApplicationValuesV2`도 받는다. v2에서는 리소스 ID별 DB 출력이 이미 해결된 문자열 또는 Secret 이름·키 참조가 `env`에 들어가며, 여러 DB를 쓰는 앱도 변수별로 구분된다. Chart JSON Schema는 v1/v2를 분기해 검증한다. 렌더는 실제 Secret 존재, 이미지 Pull, Pod Ready를 보증하지 않는다.
