# Ticket 28 — 보안 측정 모델과 원본 측정 (데모 4행)

## 상태
DRAFT — 사용자 검토 전. Issue 미생성.

## 목적
원본의 보안 상태를 기준별로 측정해 근거와 함께 `AnalysisReport`에 싣는다. 이후 판정(29)과 Sandbox 증명(30)의 입력이다.

## 관련 요구사항
AV-023

## 선행 작업
Blocked by: 04, 06, 07, 19 (모두 병합됨). 결정 필요: 보안 동등성 정책(원본 약점을 승인하면 대상에서 고쳐 배포)을 제품 정책으로 받아들일지. 이 티켓은 측정만 하므로 정책이 정해지기 전에도 진행할 수 있다.

## 작업 범위
- 포함
  - 계약: `SecurityMeasure`(기준 ID, 상태 값, `Evidence` 목록)와 `AnalysisReport.security`. 기준은 `EXTERNAL_HTTPS`, `DB_ACCESS_SCOPE`, `DB_TRANSPORT_ENCRYPTION`, `ADMIN_API_EXPOSURE`. 근거가 없으면 `UNKNOWN`.
  - 측정 규칙(순수 함수, 입력은 이미 만든 `ManifestScan`·`AwsInfraScan`·코드 스캔·`.env` 키 이름):
    - 외부 HTTPS: ALB 리스너 프로토콜·CloudFront 여부(`elbv2:DescribeListeners`는 허용 목록에 이미 있음), Ingress TLS.
    - DB 접근 범위: NetworkPolicy 유무와 보안 그룹 규칙(클러스터 안 모든 Pod가 DB에 닿는가).
    - DB 전송 암호화: 연결 설정의 `sslmode` 키·값(키 이름과 값 중 모드만 보고 비밀번호는 읽지 않음).
    - 운영용 API 노출: Ingress·ALB 라우트에 운영용 경로가 외부로 열려 있는가. 운영용 경로의 판별 기준(경로 패턴 vs 코드 라우트 스캐너 `http_routes`)은 구현 전에 정한다.
  - `AnalysisReport` 변경은 필드 추가만(기존 직렬화 호환).
- 제외: 판정·승인 연결(29), 프로브(30), 감사 로그·백업·저장 암호화·권한·시크릿 항목, 대상 구성 반영.

## 수용 기준
- [ ] 데모 앱 모양의 고정 입력에서 4개 기준이 각각 기대한 상태와 근거를 낸다.
- [ ] 근거가 없거나 스캔 결과가 없으면 `UNKNOWN`이고 통과로 취급되지 않는다.
- [ ] 같은 입력이면 같은 결과가 나온다.
- [ ] 비밀 값(비밀번호, 토큰)이 측정 결과·로그에 나타나지 않는다(AV-020).
- [ ] 보안 필드가 없는 기존 `AnalysisReport`가 그대로 읽히고, 기존 테스트가 통과한다.

## 테스트 전략
- 제안 Test Seam: 순수 함수 `measure_security(manifest_scan, aws_scan, code_scan, env_keys)`와 고정 픽스처(`tests/fixtures/analysis/`). 잡는 것: 기준별 판정, `UNKNOWN` 처리, 마스킹. 못 잡는 것: 실제 AWS 설정(06의 실제 계정 실행으로 1회 확인).
- 실행: `uv run pytest tests/analysis/test_security_measure.py -q`

## 문서
spec AV-023, plan(계약 초안에 `security` 추가), tasks.md를 같은 PR에서 갱신한다.

## 결정 대기
- 운영용 API의 판별 기준: 앱 전용 경로 목록을 코드에 두지 않는다. 라우트 스캐너(`http_routes`)와 Ingress에서 외부로 열린 경로를 근거와 함께 후보로 올리고, 운영용 여부는 사용자가 확정하는 방식(AV-022과 같은 흐름)을 제안한다. 28은 `security-posture` Skill 없이 결정적 후보(Ingress로 외부에 열린 경로)만 쓰고, Skill 기반 후보 제안은 티켓 30에서 더한다.
- `DB_TRANSPORT_ENCRYPTION`을 `.env` 값에서 읽는 범위(키 이름이 `sslmode`인 값의 모드만)
