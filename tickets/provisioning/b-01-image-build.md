# Ticket B-01 — S3 소스 공통 빌드와 ECR Digest 반환

## 상태

DONE — [Issue #17](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/17). 실제 AWS 계정 검증 범위는 아래에 명시한다.

## 결정과 책임

[ADR-003](../../docs/decisions/ADR-003.md)에 따라 코드 수정 뒤 이미지를 빌드하고 그 Digest를 Sandbox가 검증한다. 민서 담당 공통 모듈은 S3 아카이브 다운로드·검증, Sandbox 밖 빌드, ECR push, Digest와 로그 위치 반환까지다. Sandbox 담당자가 소스 업로드·수정 루프·모듈 호출·이미지 교체·통과 판정을 맡는다.

## 입력·출력

- 입력: 작업/시도 ID, SHA-256 이름의 `s3://.../<sha256>.tar.gz` 소스 참조와 선택적 VersionId, ECR 저장소 URI, 대상 `linux/amd64` 또는 `linux/arm64`, 빌드 로그 S3 목적지. Dockerfile·빌드 컨텍스트는 아카이브 루트에 둔다.
- 출력: 성공/실패, 소스 SHA-256·시도/플랫폼 출처, 성공 시 ECR manifest Digest와 `repository@sha256:...` 전체 참조, 로그 S3 참조와 가능한 VersionId, 실패 시 안전한 오류 코드.
- Builder의 IAM 권한으로 S3 읽기/로그 쓰기와 ECR 조회·push를 수행한다. ECR 저장소는 미리 만들고 태그 불변(immutable)으로 설정한다. 인증 값은 함수 인자나 Temporal 이력에 싣지 않는다. 로그는 원시 Docker 출력 대신 상태·Digest·안전한 오류 코드의 구조화 기록이다.

## 수용 기준

- [x] S3에서 원본 아카이브를 내려받고 파일명 SHA-256과 실제 바이트를 비교한다. VersionId가 전달되면 해당 버전을 조회한다.
- [x] 경로 이탈·심볼릭 링크·`.env`·인증 파일을 차단하고 Dockerfile/빌드 컨텍스트를 임시 디렉터리에 안전하게 푼다.
- [x] 대상 아키텍처로 build/push 후 ECR에서 manifest Digest를 조회·대조하여 Digest 고정 URI를 반환한다.
- [x] 빌드 시도 결과 로그를 S3에 기록하고, 실패에는 안전한 오류 코드·로그 참조를 반환한다.
- [x] 동일 시도 재호출 시 입력 해시를 다시 확인하고 ECR에 있는 결과를 재사용한다.
- [x] 계약·회귀 테스트와 격리 Registry build/push 검증 결과를 기록한다. 실제 AWS 계정 검증을 못하면 명시한다.
- [x] 관련 SDD와 Issue를 코드에 맞게 갱신한다.

## 제외

Dockerfile 생성, 코드 수정·소스 업로드, Sandbox 실행·검증, 최상위 Workflow 연결, Planning의 통과 Digest 대조, 승인·Production 배포. 이는 기존 통합/실행 작업에서 연결하며 B-01 완료 조건이 아니다.

## Test seam

공통 빌드 함수의 공개 입력/결과, AWS CLI/Docker 호출 계약, 격리 Registry의 실제 build/push. 대상 환경 Pull/Ready는 배포 티켓에서 검증한다.

## 호출 예시

`cmp_worker.image_build.BuildRequest`를 생성해 `build_and_push(request)`를 호출한다. 이 함수는 Temporal Activity가 아니며 Sandbox 담당자의 Activity에서 호출할 공통 라이브러리다.

```python
from cmp_worker.image_build import BuildRequest, build_and_push

request = BuildRequest.model_validate(
    {
        "migration_id": "migration-001",
        "attempt_id": "build-001",
        "source": {
            "uri": "s3://source-bucket/migration-001/<sha256>.tar.gz",
            "version_id": "optional-s3-version",
        },
        "ecr_repository": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/demo",
        "platform": "linux/amd64",
        "log_destination": {"uri": "s3://logs-bucket/migration-001/build-001.json"},
    }
)
result = build_and_push(request)
# SUCCEEDED -> result.digest, result.image_uri, result.log
# FAILED -> result.error_code, result.log (로그 업로드 실패 시 None)
```

작업/시도 ID는 호출자가 고유하게 할당한다. 동일 입력 재시도는 S3 객체 바이트를 다시 확인하고 ECR 불변 태그에 이미 push된 Digest를 재사용한다. 리포지토리 생성·IAM 설정·대상 클러스터의 ECR Pull 권한은 별도 운영/배포 설정이다.

## 검증 결과 (2026-11-03)

- `UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: Ruff 검사·포맷 검사와 pytest 196개 통과. 기존 테스트가 요구하는 Helm CLI 4.3.1을 로컬에 설치한 뒤 전체 검사를 재실행했다.
- 전용 로컬 Registry(`127.0.0.1:5502`, 검사 후 컨테이너 제거)에서 Docker Buildx `linux/arm64` build/push 후 manifest HEAD와 Buildx 메타데이터 Digest 일치: `sha256:c7a7e2bb642f149fd401ae4667ea154a6393cd4461e6e92761baa8c5587b275f`.
- `tests/test_image_build.py`: S3/ECR CLI 계약을 격리해 정상·재시도·입력 변조·위험 아카이브·mutable ECR 거부를 검증했다.
- 실제 S3 버킷/ECR 저장소와 IAM 테스트 권한은 제공되지 않아 AWS 서비스 통합 호출, 대상 클러스터 Pull/Ready는 미검증이다. ECR 저장소는 `IMMUTABLE` 태그 설정이 필요하다. Docker 원시 출력은 로그에 저장하지 않고 안전한 상태·오류 기록만 남긴다.
