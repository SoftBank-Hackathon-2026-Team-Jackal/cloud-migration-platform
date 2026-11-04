# k8s-workload

## 언제 쓰는지
Deployment·StatefulSet의 컨테이너 실행 요구, ServiceAccount·IRSA, StorageClass·PVC, 사이드카·initContainer를 매핑할 때 쓴다. 근거는 매니페스트 스캔 결과(`app.yaml#Deployment/<이름>/...`)다.

## 조사 절차
1. 워크로드마다 첫 번째 컨테이너를 앱으로 보고 이미지·포트·자원·probe·replica를 확인한다.
2. 두 번째 이후 컨테이너와 initContainer를 변환 못 한 항목으로 분류하고, 앱 동작에 필요한지 근거 주변을 확인한다.
3. ServiceAccount의 `eks.amazonaws.com/role-arn`이 있으면 앱이 클라우드 API를 부르는지 코드 스캔 결과와 교차 확인한다.
4. PVC·StorageClass가 영속 데이터를 담는지, 임시 저장인지 구분한다.

## 판단 기준
- 이미지·포트·자원 요구는 대상에 그대로 쓸 수 있으면 `DIRECT`.
- IRSA는 앱이 AWS API를 부를 때만 필수 후보다. 부르지 않으면 선택으로 제안한다.
- StorageClass가 EBS이면 대상별로 바꿔야 하므로 `API_DIFF` 이상이다.
- 근거가 없는 항목은 만들지 않는다.

## 대상별 차이
- AWS: IRSA·EBS를 그대로 쓴다.
- GCP: Workload Identity와 Persistent Disk로 바꾼다.
- ON_PREM(K3s): 클라우드 IAM 신원이 없고 기본 StorageClass는 로컬 저장소 수준이다.

## 수정 패턴
- 요구사항 수정: 선언되지 않은 replica·자원·포트는 매니페스트 값으로 채운다.
- 코드 수정이 필요한 경우(예: AWS SDK 호출 제거)는 Fixer가 제안하고 사람이 검토한다.

## 테스트 패턴
- 배포 직후 헬스 체크 경로가 응답하는지 확인한다.
- 앱 재시작 후 같은 응답을 하는지 확인한다.
- 볼륨을 쓰면 재시작 후 데이터가 유지되는지 확인한다.
