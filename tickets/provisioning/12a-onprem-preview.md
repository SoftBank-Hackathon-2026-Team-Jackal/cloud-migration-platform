# Ticket 12A — 온프레미스 K3s Plan Preview 생성

## 상태
IN_PROGRESS — [Issue #41](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/41)

## 목적
ON_PREM 계획도 AWS/GCP처럼 사용자가 승인 전에 볼 수 있는 Preview를 자동으로 만든다. 지금은 공통 `Preview` 형식만 있고 ON_PREM 생성기가 없어 Planning이 직접 채워야 했다(12 검증 중 확인된 빈 부분).

## 관련 요구사항
PROV-009, PROV-014, PROV-015, PROV-021

## 선행 작업
Blocked by: 04B, 07, 12 (모두 DONE)

## 작업 범위
- 포함: 승인 대상 `PlanManifest`(ON_PREM, WORKLOADS/FULL)와 그 manifest가 고정한 DB·앱 Values 바이트로 `Preview`를 만드는 순수 함수. 값 바이트를 ArtifactRef(SHA-256·크기)로 검증하고, 대상·이미지 불일치는 Preview 오류로 표시해 승인을 막는다.
- 제외: 클러스터 조회(기존 Release·PVC 존재 여부), Planning 저장 경계 연결, 프론트 화면, Chart 템플릿 변경.

## 수용 기준
- [ ] 정상 ON_PREM 계획의 Preview가 대상 클러스터·Agent·Namespace, DB(사양·PVC 크기·StorageClass), 앱(이미지·Replica·자원·포트·헬스 경로·환경변수 개수), 외부 노출 없음, 적용 기본값을 보여 주고 공통 승인 계약을 통과한다.
- [ ] 대상(Namespace·Provider·환경)이나 이미지가 manifest와 다르면 Preview `errors`로 표시되어 승인이 거절된다.
- [ ] Values 바이트가 manifest의 Digest와 다르거나 ON_PREM이 아니면 Preview를 만들지 않는다.
- [ ] Preview에 환경변수 값·Secret 값이 들어가지 않는다(키 이름·Secret 이름만).

## 착수 결정 (2026-11-04)

| ID | 결정 | 근거 |
| --- | --- | --- |
| F1 입력 | `onprem_preview(manifest, artifacts: dict[role, bytes])`. Planning 또는 Activity가 저장소에서 읽은 바이트를 넘긴다. 함수는 I/O를 하지 않는다. | 05/06처럼 저장·권한은 호출자, 순수 계산은 공통 모듈(shared-contracts) |
| F2 표시 범위 | 변경: DATABASE(StatefulSet), STORAGE(PVC), APP(Deployment), APP(Service), INGRESS `NO_CHANGE`(외부 공개 없음), CLUSTER `NO_CHANGE`(기존 K3s 사용). 기본값: Namespace 없으면 생성, DB 비밀번호 Secret 없으면 클러스터 안에서 생성(12 E4·E7). | 12의 Agent 실행 동작과 같은 내용을 미리 보여 준다 |
| F3 생성/갱신 구분 | Preview는 클러스터를 조회하지 않으므로 행위는 `CREATE`로 표시하고, 경고 `CLUSTER_STATE_NOT_INSPECTED`로 "기존 Release는 실행 시 소유권 확인 후 갱신"을 알린다. | 승인 전 대상 접근 없이 Preview 생성(PROV-014) |
| F4 비밀 | 환경변수는 개수와 Secret 참조 개수만, 사용자 Secret은 이름·키만 경고 `USER_SECRET_REQUIRED`로 표시한다. 리터럴 값은 넣지 않는다. | PROV-021 |
| F5 오류 | Values의 환경·Provider·Namespace 불일치 `VALUES_TARGET_MISMATCH`, 앱 이미지와 manifest `image_uri` 불일치 `IMAGE_MISMATCH`, StorageClass 두 값 불일치 없음 확인. 오류가 있으면 07 `approval_issues`가 `VALIDATION_FAILED`로 거절한다. 바이트 Digest 불일치·비 ON_PREM은 Preview 자체를 만들지 않는다(`ProvisioningInputError`). | 공통 승인 계약 재사용 |

## 테스트 전략
- Test Seam: 04B 실제 렌더로 만든 Values 바이트 + ON_PREM manifest → `onprem_preview` → `Preview`와 `approval_target` 결과. 12의 테스트 지원 코드(`tests/onprem_rollout_support.py`)를 재사용한다.
- 잡는 것: 표시 내용, 대상·이미지 불일치 차단, Digest 불일치 거절, 비밀값 미노출, 승인 계약 통과.
- 놓치는 것: 실제 클러스터의 기존 Release·PVC 상태, 화면 표시, Planning 연결.

## 관련 문서
- [공유 계약](../../specs/provisioning/shared-contracts.md) · [12 티켓](12-k3s-approved-rollout.md) · [07 티켓](07-approval-gate.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
