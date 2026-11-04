# 예: 외부 API 호출과 ALB 진입

## 입력
- 코드 스캔: `EXTERNAL_HOST api.payments.example.com main.go:22`
- 매니페스트: `app.yaml#Ingress/shop/metadata/annotations/alb.ingress.kubernetes.io/scheme`

## 기대 출력
- `network-policy` 행: 외부 호스트 호출 후보, 이전 대상이면 `MIGRATE`, 원본 서비스면 `KEEP`.
- `ingress` 행: ALB 어노테이션이라 AWS `DIRECT`/GCP·ON_PREM `API_DIFF`, 외부 진입은 분석 결과에 기록.
