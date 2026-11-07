# Ticket 84 — 데이터 이전 DB 권한을 앱 Namespace와 함께 만들어, 환경을 다시 만들어도 권한이 빠지지 않게 한다

## 상태
IN_REVIEW (로컬 구현·시험, 실제 k3d 권한 확인 완료, 노트북 Agent 재설치 대기). 75(데이터 이전 kubeconfig)의 후속.

## 배경 (2026-11-07 `migration-ea0f1100`)
- 데이터 단계의 터널 구성에서 `TUNNEL_COMMAND_FAILED`로 멈췄다. 대상 k3d `cmp-app`에 NetworkPolicy를 적용하다 거부됐다.
- 75의 DB 권한(Role·RoleBinding)은 `cmp-app` 안에 있다. `apply`는 그 Namespace가 이미 있을 때만 만들고, 없으면 종료 코드 3과 함께 "배포 뒤 `bind`" 안내만 남긴다. 이날 새 Agent 등록 뒤 `apply`가 `cmp-app`보다 먼저 실행됐고 `bind`는 실행되지 않았다.
- 같은 일이 반복되는 구조였다. 정리(`delete_data`)나 수동 삭제로 `cmp-app`이 지워지면 그 안의 권한도 함께 지워지고, 다음 롤아웃이 Namespace를 다시 만들어도 권한은 돌아오지 않는다.

## 결정
| ID | 항목 | 결정 |
| --- | --- | --- |
| D1 | DB 권한 위치 | 75의 DB 권한 표를 ClusterRole `cmp-data-migration-db` 하나로 둔다. ClusterRoleBinding은 없으므로 RoleBinding이 있는 Namespace에서만 효력이 있다(권한 범위는 75와 같다) |
| D2 | 연결 주체 | Agent가 롤아웃에서 Namespace를 준비할 때 RoleBinding `cmp-data-migration-db`(→ ClusterRole, 대상 `cmp-data-migration/cmp-data-migration`)를 만든다. Namespace와 함께 생기고 함께 지워진다 |
| D3 | Agent 권한 | `rolebindings` get·create와 ClusterRole `cmp-data-migration-db` 하나에 대한 `bind`만 더한다. 쿠버네티스 권한 상승 방지 규칙상 다른 Role은 연결할 수 없다(실제 k3d에서 `cluster-admin` 연결 거부 확인). 이미 클러스터 전체 Secret·`pods/exec`가 있어 신뢰 범위는 늘지 않는다 |
| D4 | 기존 연결 | Agent는 연결이 이미 있으면 바꾸지 않는다(`roleRef`는 불변이고, 75 방식 연결도 같은 권한을 준다). 스크립트 `bind`는 75 방식(Role) 연결을 지우고 ClusterRole 연결로 다시 만든다 |
| D5 | 조건 없음 | Chart 값으로 끄고 켜지 않는다. 데이터 이전을 쓰지 않는 클러스터에서는 ClusterRole·SA가 없어 연결이 아무 권한도 주지 않는다 |
| D6 | 스크립트 | `apply`는 앱 Namespace가 없어도 성공(종료 코드 0)이다. `delete`는 ClusterRole도 지워 Agent가 만든 연결의 효력을 없앤다 |

## 수용 기준
- [x] 롤아웃이 새로 만든 Namespace에 데이터 이전 RoleBinding이 생긴다
- [x] 기존 연결은 바꾸지 않는다
- [x] 스크립트와 Agent가 같은 이름·대상·roleRef를 쓴다(시험으로 묶음)
- [x] Agent 권한으로 그 ClusterRole만 연결할 수 있다(실제 k3d)
- [ ] 노트북 Agent 재설치, `cmp-app`을 지운 뒤 롤아웃 → 데이터 단계 통과 확인
