# 예: IRSA 서비스 계정과 EBS 볼륨

## 입력
- `app.yaml#ServiceAccount/api/metadata/annotations/eks.amazonaws.com/role-arn`
- `app.yaml#StorageClass/gp3/provisioner` = `ebs.csi.aws.com`
- 코드 스캔: AWS SDK 호출 없음

## 기대 출력
- `pod-identity` 행: 등급 AWS `DIRECT`/GCP `API_DIFF`/ON_PREM `NONE`, 앱이 AWS API를 부르지 않으므로 `required_proposed=false`, 근거는 매니페스트 경로.
- `volume` 행: EBS 의존이라 대상별로 바꿔야 함(`API_DIFF` 이상), 영속 데이터를 담으면 필수 제안.
