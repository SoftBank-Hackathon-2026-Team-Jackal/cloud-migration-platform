# Ticket 12 — 원본 NetworkPolicy가 chat-app → PgBouncer 연결을 막는 문제

## 상태

DONE(2026-11-07). 서버 dry-run 뒤 원본 EKS `cmp-demo-source`에 적용(사용자 승인). chat-app 재시작 없음, 공개 헬스 200.

## 결론

원본 `chat-source`에 허용 정책 `cmp-allow-chat-app-to-cutover`(chat-app → `app.kubernetes.io/name=cmp-cutover`, TCP 6432)를 **더한다**. 기존 `chat-app-egress`는 그대로 둔다.
매니페스트와 적용·되돌리기 방법: [`infra/data-migration/source-prep/`](../../infra/data-migration/source-prep/README.md).

## 문제

- `route_app`이 streaming 직후 `chat-secrets` `DATABASE_URL`을 `cmp-pgbouncer.chat-source.svc:6432`로 바꾸고 chat-app을 재시작한다.
- 원본 `chat-app-egress`는 chat-db:5432와 DNS만 허용하고, 원본 EKS VPC CNI는 정책을 강제한다.
- 결과: 새 chat-app Pod가 PgBouncer에 못 붙어 뜨지 못한다. Ticket 11 검증은 정책이 없는 `cmp-dm-src`에서 해서 드러나지 않았다.

## 확인

- PgBouncer → chat-db, 앱 → PgBouncer 수신: 막는 정책 없음(`cmp-cutover`·`chat-db`를 고르는 정책 없음).
- 플랫폼 코드(`packages/data`, `apps/worker`)는 바꾸지 않았다. 정책은 원본 사전 준비로 한 번 적용한다.

## 적용 결과

| 항목 | 적용 전 | 적용 후 |
| --- | --- | --- |
| `chat-source` NetworkPolicy | `chat-app-egress` | `chat-app-egress`(그대로), `cmp-allow-chat-app-to-cutover` |
| AWS PolicyEndpoint | `chat-app-egress-*` | 기존 + `cmp-allow-chat-app-to-cutover-*` 생성 |
| chat-app Pod | `chat-app-b85df74fd-q5t5r` Running, 재시작 0 | 같은 Pod(UID 동일), Ready, 재시작 0 |
| chat-db Pod | `chat-db-0` Running, 재시작 0 | 같음 |
| 공개 헬스(CloudFront `/healthz`) | 200 | 200(3회) |
| DB를 읽는 API(`/api/stats`, `/api/messages`) | — | 200(기존 chat-db 연결 유지 확인) |
| chat-app 로그 오류(적용 뒤 3분) | — | 0건 |

## 되돌리기

```bash
kubectl -n chat-source delete networkpolicy cmp-allow-chat-app-to-cutover
```

## 남은 일

- 실제 전환 때 `route_app` 뒤 새 chat-app Pod가 Ready가 되는지 확인(데이터 2안 검증에서).
- 플랫폼이 다른 원본 앱에도 쓰려면 이 정책을 `install_cutover` 단계에서 자동으로 만들지 결정 필요(Data 담당, 이번 범위 아님).
