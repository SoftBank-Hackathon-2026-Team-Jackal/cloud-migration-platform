# 예: 확장 사용과 단일 URL 연결

## 입력
- SQL 스캔: `db/001.sql:3` `CREATE EXTENSION pgcrypto`
- 코드 스캔: `ENV_READ DATABASE_URL main.go:13`, `DB_DRIVER github.com/jackc/pgx/v5`

## 기대 출력
- `postgres` 행(확장): 등급 `NONE`, 잃는 것 `확장 기능`, 필수 제안이면 차단 사유, 근거 `db/001.sql:3`.
- `postgres` 행(연결): 단일 URL 변수이므로 `DATABASE_URL`을 `connection_url_secret_ref`로 바인딩하고 근거 `main.go:13`을 남긴다(원본 URL 값은 쓰지 않는다).
