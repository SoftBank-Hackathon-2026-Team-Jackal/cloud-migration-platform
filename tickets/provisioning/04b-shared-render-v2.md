# Ticket 04B — Sandbox·Provisioning 공통 렌더링 v2

## 상태

DONE — [Issue #23](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/23). 코드·문서·로컬 렌더 검증 완료; main 병합 여부는 PR에서 확인한다.

## 목적과 범위

사용자가 확정한 [공통 배포 계약 v2](../../specs/provisioning/onprem-shared-deployment.md#v2-공통-렌더-입력반환)의 `resources[]`를 실제 DB/App Chart Values·Manifest로 렌더한다. Worker는 대상 Namespace·Release·StorageClass·이미지·최종 Secret 참조를 결정해 전달한다. 공통 모듈은 리소스 ID 연결과 렌더를 수행한다.

기존 v1 단일 앱/DB 렌더 계약은 유지한다. `.env` 원문·mapping_ref 처리, Secret 생성, 대상 capabilities/DB 이미지 내용 조회, Kubernetes Apply/Ready, Terraform/Plan/승인/Temporal 연결은 범위 밖이다.

## 공개 Test Seam

`render_shared_deployment_v2(payload, database_chart_dir=..., application_chart_dir=...)`의 타입화된 DB/App별 Values·Manifest·resolved_env_bindings 반환과 `ProvisioningInputError.issues`. Helm template 결과에서 리소스 ID별 Release/DB Service/Secret 참조를 확인한다. 네트워크나 클러스터 접근 성공의 증거로 취급하지 않는다.

## 수용 기준

- [x] 여러 APPLICATION/POSTGRESQL을 고유 ID로 렌더하고 앱별 DB 출력·Secret 참조가 정확히 결합된다.
- [x] 미지원 종류/DB 확장, 중복·누락 참조, 비밀값 원문, 잘못된 이미지/이름/플랫폼은 안전한 오류로 거절된다.
- [x] SANDBOX/PRODUCTION·AWS/GCP/ON_PREM 입력 계약이 같은 코드 경계를 사용하고 기존 v1 회귀가 유지된다.
- [x] `make check`와 Helm 템플릿 검증이 통과하며 SDD·티켓이 결과와 일치한다.

## 실행 증거와 남은 경계

- 2026-11-04: `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` — Ruff lint/format 통과, pytest **254 passed**(기존 Starlette 경고 1건). v1 렌더 회귀를 포함한다.
- `helm lint` DB/App Chart 각각 0 failed, v2 Values로 `helm template` 두 Release 성공. Helm lint는 CLI 기본 Release 이름 때문에 정보성 fail 로그를 출력했으나 lint 종료·명시적 Release template 모두 성공했다.
- `tests/test_shared_deployment_v2.py`: 다중 앱·DB의 서로 다른 연결, SANDBOX/PRODUCTION·AWS/GCP/ON_PREM, 실제 Manifest의 Service/Secret/이미지, 미지원/오류·Helm stderr 비노출 확인.
- 실제 Secret/StorageClass 존재, 이미지 안의 PostgreSQL 16 여부와 ECR Pull 권한, 대상 capabilities, Kubernetes Apply/Ready·테스트, Planning Spec→v2 렌더 입력의 Worker 변환은 미검증·별도 범위다. AWS/GCP 계정이나 클러스터는 호출하지 않았다.
