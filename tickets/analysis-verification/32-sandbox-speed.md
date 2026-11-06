# Ticket 32 — Sandbox 종료 지연 줄이기

## 상태
IN_PROGRESS — 구현·테스트 완료, Issue 번호와 PR 대기(`tasks.md`의 Issue 칸은 미생성)

## 목적
Sandbox가 통과한 뒤 Planning으로 넘어가기까지 기다리던 시간을 줄인다. 두 가지만 한다: 네임스페이스 삭제를 기다리지 않는다, 공유 이미지를 노드에 미리 받아 둔다.

## 관련 요구사항
AV-016(정리), AV-015

## 선행 작업
Blocked by: 11, 21, 24 (병합됨).

## 구현
- **삭제를 기다리지 않음:** 루프 끝의 `sbx_teardown`이 `wait: false`로 `kubectl delete namespace --wait=false`만 보낸다. 요청 자체가 실패하면 지금처럼 `cleanup_failed`가 된다. 요청이 받아들여졌으면 정리 상태는 `CLEANED`다(삭제 요청이 접수됐다는 뜻. 이 의미는 아래 결정 대기에 적는다). 수동 정리(`sandbox_ctl.py cleanup`)와 만료 정리는 그대로 끝까지 기다린다.
- **만료 정리 Schedule:** `SandboxExpireWorkflow`가 15분마다 `sbx_cleanup_expired`(sandbox 큐, 기존 `cleanup_expired`)를 부른다. 겹치면 건너뛰고(SKIP), 워커 시작 시 한 번 등록한다(이미 있으면 유지, 실패해도 시작은 막지 않음). 2시간 만료 라벨만으로는 아무도 지우지 않으므로, 이 Schedule이 삭제를 받아들였지만 끝나지 않은 네임스페이스의 안전망이다.
- **이미지 사전 적재:** `uv run python scripts/sandbox_ctl.py prewarm`이 postgres와 egress 프록시 이미지(둘 다 digest 고정, 공개)를 `docker exec k3d-cmp-sandbox-server-0 crictl pull`로 노드에 받는다. 반복 실행해도 안전하다. 앱 이미지는 첫 배포에서 노드에 캐시된다.

## 작업 범위
- 포함: 위 구현, 테스트, runbook.
- 제외: DB `emptyDir`(재시작·유실 시험이 볼륨에 의존), DB·앱 동시 설치(앱이 DB 없이 죽을 수 있음), 준비 검사 간격(공유 차트 변경은 Provisioning과 합의 필요).

## 수용 기준
- [x] 루프 끝 정리는 `wait: false`를 보내고, 직접 삭제 호출은 `--wait=false`만 한다(남았는지 확인하지 않음).
- [x] 기다리는 정리(수동·만료 정리)는 그대로다.
- [x] 만료 정리 Schedule이 한 번만 등록되고 겹침을 건너뛰며, 등록 실패가 워커 시작을 막지 않는다. `sbx_cleanup_expired`가 Sandbox 루프 Activity에 등록된다.
- [x] `prewarm`이 두 이미지를 노드에 받는다.
- [ ] 실제 k3d에서 통과 후 대기 시간이 줄었는지 한 번 잰다(미확인).

## 테스트 전략
Test Seam: `SandboxService`(기록하는 `FakeRunner`), Sandbox 루프 Workflow(가짜 Activity), Schedule 등록(가짜 클라이언트), `prewarm_images`(가짜 러너). 잡는 것: 삭제 방식, 호출 인자, Schedule 정책, 등록 실패 처리. 못 잡는 것: 실제 삭제 시간과 클러스터의 삭제 완료 여부.

## 결정 대기
- 정리 상태 `CLEANED`의 의미를 "삭제 완료"에서 "삭제 요청 접수(안전망 있음)"로 넓힌다. Planning은 `CLEANED`가 아니면 정리 정책 결정(`CLEANUP_POLICY_REQUIRED`)을 요구하므로, 이 의미가 Provisioning 쪽 기대와 맞는지 확인이 필요하다.
- 데모처럼 검토 대기로 2시간 넘게 멈춘 Sandbox는 만료 정리 대상이 된다(기존과 같은 한계).
