# ENV-01 — .env 업로드 API·암호화 저장·삭제 정책

## 담당
민서

## 상태
IN_PROGRESS — [#145](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/145). 구현·테스트 완료, 조정 세션 검토 대기(push·PR 전).

## 착수 결정 (2026-11-05, 사용자 승인)
- 저장 위치(결정 3): 산출물 저장소 방식의 파일 저장 + 암호화. 플랫폼 디렉터리(`ENV_FILE_STORE_DIR`) 아래 파일 0600·디렉터리 0700. 업무 DB는 바꾸지 않는다(Alembic 변경 없음).
- 암호화: AES-256-GCM(`cryptography` 46.0.x, `>=44,<47`로 고정·`uv.lock` 기록). 표준 AEAD이고 Python 표준 라이브러리에 AEAD가 없어 의존성을 추가한다. 버전마다 무작위 96비트 nonce, 연결 데이터 `id|version|sha256`로 다른 참조에 암호문을 바꿔 끼울 수 없게 한다. Fernet(AES-CBC+HMAC)보다 연결 데이터를 직접 쓸 수 있어 AES-GCM을 골랐다.
- 키: 플랫폼 설정 `ENV_FILE_ENCRYPTION_KEY`(base64url 32바이트, 서버 `.env`만, 저장소 밖). 키 교체는 후속.
- 보관 기간(결정 2): 이전 종료 7일 뒤 삭제 + 명시적 `DELETE`. 삭제 뒤 읽기는 `ENV_FILE_EXPIRED`.
  - 이전에 한 번도 쓰이지 않은 업로드는 마지막 버전 업로드 7일 뒤 삭제(구현 중 정한 세부, 승인 규칙의 연장).
  - 삭제 트리거: `scripts/purge_env_files.py`가 저장된 파일의 이전 ID로 Temporal `close_time`을 조회해 지운다(Temporal에 없는 Workflow는 이미 보관 기간이 지난 것으로 보고 즉시 삭제). 주기 실행(cron 등) 등록은 배포 후속이다. Workflow 종료 시점 Activity 훅은 두지 않았다(저장소 키를 워커에 배포하지 않아도 된다).
- 위치: 저장소 클래스는 워커(ENV-04~06)도 읽어야 해 `apps/api`가 아니라 `cmp_providers.env_file_store`에 둔다. ENV-03의 `cmp_providers.env_file`(프로토콜·공통 함수)과 파일을 나눈다.
- 오류 형식: ENV-03이 아직 병합되지 않아 `EnvFileError(code)`를 이 모듈에 정의했다. 병합 때 ENV-03 오류와 하나로 합친다.

## 목적
웹에서 올린 `.env` 원본을 플랫폼이 암호화해 하나만 저장하고, 이후 단계가 쓸 참조 `env_file_ref`를 돌려준다. 원문은 저장소 밖으로 나가지 않는다.

## 관련 요구사항
ENV-001, ENV-002, ENV-003 ([명세](../../specs/env-file-ref/spec.md)). PROV-004, PROV-021.

## 선행 작업
없음. 저장소 인터페이스(`EnvFileStore`)는 ENV-03과 먼저 맞춘다.

## 범위
- 포함
  - `POST /env-files`(운영자 키, 웹 서버 전용): 본문은 `.env` 원본 바이트(상한 64 KiB, UTF-8). 응답 `{env_file_ref: {id, version, sha256}, keys: [이름]}`. 값은 응답하지 않는다.
  - 같은 `id`에 다시 올리면 `version`을 올린다(`POST /env-files/{id}`). 이전 버전은 보관 기간까지 남긴다.
  - `DELETE /env-files/{id}`와 보관 기간이 지난 원본 자동 삭제. 삭제된 참조를 읽으면 `ENV_FILE_EXPIRED`.
  - 암호화 저장 구현(결정 3), 암호화 키는 플랫폼 설정(서버 `.env`, 0600)에서 읽는다.
  - 형식 검증: 빈 줄·주석·`export` 접두사·따옴표 처리는 분석의 `cmp_scanners.envfile.parse_env_names`와 같은 규칙. 중복 키는 마지막 값이 이기고 경고 코드로 알린다.
  - OpenAPI, SDD 갱신
- 제외: 웹 업로드 화면, 외부 Secret Manager, 값 조회 API(값은 `render_env`로만 읽는다)

## 수용 기준
- [x] 업로드한 원본이 암호화되어 하나만 저장되고, 저장된 바이트에서 원문 문자열이 보이지 않는다.
- [x] 응답·로그·예외 메시지에 값이 없다. 422 응답도 입력 값을 되돌리지 않는다.
- [x] `sha256`은 원본 바이트 기준이라 같은 파일은 같은 값이다.
- [x] 삭제·만료 뒤 읽기는 `ENV_FILE_EXPIRED`다.
- [x] 운영자 키 없이 호출할 수 없다.

## 구현 기록
- 코드: `cmp_providers.env_file_store`, `cmp_api.routes.env_files`, 설정 `env_file_store_dir`·`env_file_encryption_key`, `scripts/purge_env_files.py`. 계약 요약은 [shared-contracts](../../specs/provisioning/shared-contracts.md#env-업로드와-env_file_ref-env-01env-02).
- 테스트: `tests/test_env_files.py`(업로드·암호문 검사·0600·버전·삭제·만료·변조·운영자 키·파싱 규칙).
- 검증하지 않은 것: 실제 서버에서 cron으로 purge 실행, 다중 프로세스 동시 업로드(버전 파일은 `O_EXCL`로 덮어쓰지 않지만 메타 갱신은 마지막 쓰기가 이긴다).

## Test Seam
FastAPI TestClient + SQLite + 임시 디렉터리. 원문 비노출은 응답·로그 캡처 문자열 검사로 확인한다.

## 결정 필요
- 2. 보관 기간(운영권 인계 후 삭제 / 이전 종료 후 N일)
- 3. 저장 위치(업무 DB 암호화 컬럼 / 산출물 저장소 0600 + 암호화)와 암호화 키 보관 위치
