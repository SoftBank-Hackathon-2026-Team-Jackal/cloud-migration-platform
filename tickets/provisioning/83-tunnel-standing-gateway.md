# Ticket 83 — 데이터 터널 원본 게이트웨이도 고정해 handshake 대기(약 30초)를 없앤다

## 상태
IN_REVIEW (로컬 구현·시험 완료, 고정 게이트웨이 생성·리허설 대기). 82(고정 NLB)의 후속. **데이터 영역 코드(`cmp_data.tunnel`, `data_tunnel` Activity) 대행 — 데이터 담당 공유 필요.**

## 배경 (2026-11-07 `cmp-wg-2aa45ef7`, 원본 EKS 실측)
- 82 이후에도 터널 연결에 약 30초가 걸렸다. 원본 게이트웨이 Pod Ready 17:02:24 KST, NLB `RegisterTargets` 17:02:25(CloudTrail), HealthyHostCount 0→1은 17:02 구간, 첫 `NewFlowCount_UDP`는 17:03 구간(CloudWatch).
- 실행마다 원본 게이트웨이 Pod를 새로 만들면 NLB가 새 Pod IP를 대상으로 등록하고 healthy로 판정할 때까지 handshake UDP를 버린다. 헬스체크 단축(82 D6)으로는 대상 등록 지연이 줄지 않는다.

## 결정
| ID | 항목 | 결정 |
| --- | --- | --- |
| D1 | 원본 게이트웨이 | `standing_ingress`이면 원본 게이트웨이를 고정 Deployment `cmp-wg-standing`(키 Secret 같은 이름)으로 둔다. Pod 템플릿에 실행별 값이 없어 다시 apply해도 Pod가 바뀌지 않는다 |
| D2 | 실행별 설정 | `kubectl exec`로 `/run/cmp/peer`(emptyDir)에 `<실행 이름> <대상 공개키> <원본 DB 주소>`를 쓴다. 게이트웨이는 1초마다 읽어 이전 peer·원본 DB 리스너를 지우고 새로 건다 |
| D3 | 순서 | 원본 peer를 먼저 바꾸고 대상 게이트웨이를 띄운다(첫 handshake가 거절되지 않게). handshake 확인 간격 3초 → 1초 |
| D4 | NLB | 고정 NLB selector는 항상 고정 게이트웨이. 쓰는 실행은 Service annotation `cmp.dev/data-tunnel-run`(쉬면 `standing-idle`)으로 표시하고 스크립트의 사용 중 판단 근거로 쓴다 |
| D5 | 정리 | 대상 게이트웨이·키·정책과 원본 `<name>-peer-db` Service만 지운다. 원본 peer는 그 실행의 설정일 때만 비우고 annotation을 `standing-idle`로 돌린다 |
| D6 | 미리 만들기 | `make data-tunnel-nlb-up`이 키 Secret(없을 때만)·고정 게이트웨이·NLB를 만들고 NLB 대상 `healthy`까지 기다린다. `down`은 고정 게이트웨이도 지운다 |
| D7 | 미리 만들지 않은 경우 | 실행이 고정 게이트웨이를 만든다. 그 실행만 대상 등록을 기다리고(주소 대기 예산까지), 다음 실행부터 빠르다 |

## 수용 기준
- [x] standing 실행은 원본에 실행별 Deployment를 만들지 않고 고정 게이트웨이 peer만 바꾼다
- [x] 정리 뒤 고정 NLB·고정 게이트웨이·키가 남고, 다른 실행의 peer는 건드리지 않는다
- [x] 고정 게이트웨이 시작 스크립트가 peer 없이 뜨고, 설정 파일이 바뀌면 이전 peer를 지우고 새 peer를 건다
- [x] 스크립트 `up`이 NLB 대상 healthy까지 기다린다
- [ ] 원본 EKS에 고정 게이트웨이 생성, worker 재배포, 리허설에서 handshake 시간 확인(별도 승인)

## 검증
`make check` 통과(3499 passed). HTTP·DB 계약 변경 없음(`tunnel` 입력 그대로).
