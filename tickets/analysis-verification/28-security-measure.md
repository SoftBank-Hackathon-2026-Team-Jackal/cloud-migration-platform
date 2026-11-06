# Ticket 28 — 보안 측정 모델과 원본 측정 (데모 4행)

## 상태
DONE — PR #250 병합, Issue #249. 28-A(AWS 스캔에 앞단 보호 정보 추가)와 28-B(측정 모델과 원본 측정) 구현 완료(2026-11-06). 미확인: 실제 계정에서 스캔 1회, 역할 템플릿 `v3` 반영(원격 작업, 승인 필요)

## 목적
원본의 보안 상태를 기준별로 측정해 근거와 함께 `AnalysisReport`에 싣는다. 이후 판정(29)과 Sandbox 증명(30)의 입력이다.

## 관련 요구사항
AV-023

## 선행 작업
Blocked by: 04, 06, 07, 19 (모두 병합됨). 결정 필요: 보안 동등성 정책(원본 약점을 승인하면 대상에서 고쳐 배포)을 제품 정책으로 받아들일지. 이 티켓은 측정만 하므로 정책이 정해지기 전에도 진행할 수 있다.

## 28-A — AWS 스캔에 앞단 보호 정보 추가 (구현 완료)
원본의 "외부 HTTPS"를 측정하려면 ALB 리스너의 TLS 상세와 그 앞의 CloudFront가 필요한데, 스캐너에는 CloudFront가 전혀 없고 리스너는 포트·프로토콜만 있었다.
- 로드밸런서에 `dns_name`, 리스너에 `has_certificate`·`ssl_policy`·`redirects_to_https`를 더했다(`DescribeLoadBalancers`·`DescribeListeners` 응답에 이미 있는 값, 새 호출 없음).
- `cloudfront:ListDistributions`(허용 목록과 역할 템플릿에 추가)로 **클러스터 VPC의 로드밸런서를 오리진으로 쓰는 배포만** 골라 `CloudFrontInfo`(뷰어 프로토콜 정책, 별칭, 사용 여부, 일치한 오리진)를 싣는다. 모든 페이지를 읽는다.
- CloudFront는 **최선 노력**이다. 이 권한이 없는 기존 연결 역할도 스캔은 그대로 돌고, `cloudfront_status`가 `DENIED`로 남는다(`NOT_REQUESTED`: 클라이언트 없음 또는 일치할 로드밸런서 없음, `COLLECTED`). `COLLECTED`가 아니면 "배포 없음"으로 읽지 않는다.
- 새 필드는 모두 기본값이 있어 이전에 저장된 스캔 산출물도 그대로 읽힌다. `AwsClients`의 `cloudfront`는 선택이라 클라이언트 5개로도 만들어진다.
- 실제 계정 확인은 06과 함께 한다(승인 필요). 역할 템플릿 호스팅은 아래 "배포 주의" 참고.

### 배포 주의 (28-A)
- `infra/connections/source-role.yaml`의 권한이 늘었다. `infra/demo/deploy-connection-templates.sh`는 같은 버전 경로(`connections/v2/`)에 내용이 다른 템플릿을 올리려 하면 `TemplateVersion`을 올리라며 거절한다. 반영하려면 버전(`v3`)을 올리고 S3에 올리는 원격 작업이 필요하다(사용자 승인). 코드는 이 작업 전에도 동작한다.
- 이미 만든 연결의 CloudFormation 스택은 권한이 갱신되기 전까지 `cloudfront:ListDistributions`가 없어 `DENIED`가 된다. 사용자가 스택을 새 템플릿으로 갱신해야 한다.
- ADR-005의 원본 역할 권한 표(분석 스캔이 부르는 API)에 `cloudfront:ListDistributions`가 아직 없다. ADR은 이 작업 범위 밖이라 고치지 않았다(문서 반영 필요).

## 작업 범위 (28-B, 구현 완료)
- 포함
  - 계약: `SecurityMeasure`(기준, 상태 `PROTECTED` / `PARTIAL` / `UNPROTECTED` / `UNKNOWN` / `NOT_APPLICABLE`, 사유 코드, `Evidence`)와 `AnalysisReport.security`(선택 필드). 기준은 `EXTERNAL_HTTPS`, `DB_ACCESS_SCOPE`, `DB_TRANSPORT_ENCRYPTION`, `ADMIN_API_EXPOSURE`. 근거가 없으면 `UNKNOWN`이고 통과로 취급하지 않는다. 사유는 코드이고 값(연결 문자열, 주소, 비밀)은 담지 않는다.
  - 스캔 확장(새 필드는 모두 기본값이 있어 이전 산출물이 그대로 읽힌다): 매니페스트 스캔에 `Ingress`(경로, TLS), Ingress NetworkPolicy(`IngressPolicy`: 선택 Pod, 허용 출발지, 전체 허용 여부), ConfigMap·환경변수 리터럴의 `sslmode`(`SslModeFinding`). 코드 스캔에 `sslmode` 리터럴(`SslModeSite`, 언어 무관, `.env` 제외). `sslmode`는 **모드 값만** 저장한다(`cmp_scanners/sslmode.py`).
  - 측정 규칙(`cmp_scanners/security.py`의 순수 함수 `measure_security`, 앱 전용 경로·포트·이름 없음):
    - 외부 HTTPS: 인터넷 쪽 로드밸런서 리스너(HTTPS이거나 HTTPS로 리다이렉트), CloudFront 배포(뷰어 정책이 `redirect-to-https`/`https-only`만), AWS 정보가 없으면 Ingress TLS. 진입점이 모두 보호되면 `PROTECTED`, 일부만이면 `PARTIAL`, 없으면 `UNPROTECTED`, 진입점이 안 보이면 `UNKNOWN`.
    - DB 접근 범위: DB 워크로드를 선택하는 Ingress NetworkPolicy가 허용 출발지를 제한하는가(없거나 전체 허용이면 `UNPROTECTED`), 보안 그룹의 5432가 인터넷에 열려 있는가. DB가 매니페스트에 없으면 `UNKNOWN`.
    - DB 전송 암호화: `disable`·`allow`·`prefer`는 평문으로 떨어질 수 있어 약함, `require`·`verify-ca`·`verify-full`은 보호. 모드가 안 보이면 `UNKNOWN`(`.env`·Secret의 값은 읽지 않는다, AV-020).
    - 운영용 API 노출: 라우트 스캔의 경로에서 관례적 운영 경로(`admin`, `internal`, `debug`, `pprof`, `actuator`, `metrics`, `management`, `manage`)를 **후보**로 올리고, 외부 진입점이 있고 Ingress 경로가 막지 않으면 `UNPROTECTED`(`ADMIN_PATH_UNCONFIRMED`). 후보가 없으면 `NOT_APPLICABLE`, 외부 도달을 알 수 없으면 `UNKNOWN`.
  - 파이프라인: 근거 존재 검사 단계에서 측정해 보고서에 싣는다(새 AWS 호출·Activity 없음).
- 제외: 판정·승인 연결(29), 프로브(30), 감사 로그·백업·저장 암호화·권한·시크릿 항목, 대상 구성 반영, 후보가 실제 운영용인지의 확정(29), `.env`·Secret 값 읽기, Go 외 언어의 라우트 스캔(라우트가 안 보이면 `UNKNOWN`).

## 수용 기준
- [x] 4개 기준이 입력별로 기대한 상태와 근거를 낸다(기준당 표 형태 테스트).
- [x] 근거가 없거나 스캔 결과가 없으면 `UNKNOWN`이고 통과로 취급되지 않는다.
- [x] 같은 입력이면 같은 결과가 나온다(사유·근거는 정렬).
- [x] 비밀 값(비밀번호, 연결 문자열)이 측정 결과·보고서에 나타나지 않는다. `sslmode`는 모드 값만 저장한다.
- [x] 보안 필드가 없는 기존 스캔·`AnalysisReport`가 그대로 읽히고, 기존 테스트가 통과한다.
- [x] 보고서에 4개 측정이 고정 순서로 실리고 근거(`파일:줄`, 매니페스트 위치)가 소스를 가리킨다.
- [ ] 실제 계정·클러스터에서 1회 확인한다(승인 후, 06과 함께).

## 테스트 전략
- Test Seam: 순수 함수 `measure_security(manifests, infra, code, routes)`를 작은 객체로 기준별 표 테스트(`tests/analysis/test_security_measure.py`), 스캔 확장은 `tmp_path`의 작은 파일(`tests/analysis/test_security_signals.py`), AWS 앞단은 `Stubber`(`tests/analysis/test_aws_edge.py`). 잡는 것: 기준별 판정, `UNKNOWN` 처리, 모드 값만 저장, `.env` 비접근. 못 잡는 것: 실제 AWS 설정과 실제 앱의 Ingress·정책 모양(실제 1회 확인).
- 실행: `uv run pytest tests/analysis -q`, 이후 `make check`

## 문서
spec AV-023, plan(계약 초안에 `security` 추가), tasks.md를 같은 PR에서 갱신한다.

## 알려진 한계 (고도화로 남김, 2026-11-07 팀 합의)
- 외부 HTTPS는 **사용자가 접속하는 입구의 암호화**만 본다. CloudFront와 로드밸런서 사이(오리진 구간)가 HTTP인지, 인터넷에서 ALB로 CloudFront를 우회해 직접 접근할 수 있는지(보안 그룹 규칙)는 보지 않는다.
- 그래서 CloudFront가 HTTPS를 강제하는데 ALB에 인터넷 쪽 HTTP 리스너가 있으면 그 리스너를 평문 입구로 세어 외부 HTTPS가 `PARTIAL`로 나올 수 있다. 오리진 구간을 따로 보거나 CloudFront 뒤 ALB를 입구로 세지 않는 방식은 고도화에서 정한다.
- 인증서가 브라우저에서 신뢰되는지는 측정하지 않는다(암호화 여부만 본다).

## 결정 (2026-11-06)
- 운영용 API 판별: 앱 전용 경로 목록을 코드에 두지 않고, 관례적 경로를 후보로 올려 근거(`파일:줄`)를 붙인다. 후보가 실제 운영용인지는 사용자가 승인 단계(29)에서 확정한다. Skill 기반 후보 제안은 티켓 30.
- `sslmode`는 모드 값만 저장한다. 비밀이 아닌 출처(소스 코드, 매니페스트 리터럴, ConfigMap)에서만 읽고, `.env`·Secret에만 있으면 `UNKNOWN`이다.
- DB 접근 범위는 첫 사이클 기준으로 클러스터 안 PostgreSQL(Pod)과 보안 그룹의 5432를 본다. RDS 등 관리형 DB의 접근 제한은 후속이다.

## 결정 대기
- `UNKNOWN`·후보의 처리(사람 검토 항목으로 올리는 방식)는 티켓 29에서 정한다.
