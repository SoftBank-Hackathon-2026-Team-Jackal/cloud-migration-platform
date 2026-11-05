# 예: AWS 변수와 누락 변수

## 입력
- `.env` 이름: `DB_HOST`, `AWS_REGION`, `LEGACY`
- 코드가 읽는 이름: `DB_HOST`, `AWS_REGION`, `FEATURE_FLAG`

## 기대 출력
- `AWS_REGION`: 제외 후보(원본 환경에 묶임). `DB_HOST`: 바인딩으로 대체.
- `FEATURE_FLAG`: `.env`에 없음 → 누락. `LEGACY`: 코드가 읽지 않음 → 안 쓰는 변수(스캔이 완전할 때만).
