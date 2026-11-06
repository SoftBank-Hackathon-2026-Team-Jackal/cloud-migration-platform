# 원본 chat-source 사전 준비: chat-app → PgBouncer 허용 정책

## 결론

원본 EKS `cmp-demo-source`의 Namespace `chat-source`에 NetworkPolicy **하나만 더한다**:
`cmp-allow-chat-app-to-cutover`(chat-app Pod → `cmp-cutover` Pod, TCP 6432).
기존 `chat-app-egress`는 바꾸지도 지우지도 않는다. NetworkPolicy는 허용 규칙이 합쳐지므로 기존 허용(chat-db:5432, DNS)은 그대로 살아 있다.

## 왜 필요한가

- 데이터 Workflow는 streaming 직후 `route_app`(`apps/worker/src/cmp_worker/activities/data_cutover.py`)으로
  `chat-secrets`의 `DATABASE_URL`을 `cmp-pgbouncer.chat-source.svc:6432`로 바꾸고 chat-app을 재시작한다.
- 원본 `chat-app-egress`는 `app=chat-db`:5432와 kube-dns:53만 허용한다. 원본 EKS VPC CNI는 NetworkPolicy를 실제로 강제한다(`enableNetworkPolicy=true`).
- 그래서 이 정책이 없으면 재시작된 새 chat-app Pod가 PgBouncer에 못 붙어 뜨지 못한다.

## 확인한 것(코드·클러스터)

| 항목 | 값 | 근거 |
| --- | --- | --- |
| PgBouncer Pod 라벨 | `app.kubernetes.io/name=cmp-cutover` | `packages/data/src/cmp_data/cutover.py` `NAME`, `LABELS` |
| Namespace | 원본 앱과 같은 `chat-source` | `manifests(app.namespace, ...)` |
| 포트 | Service `cmp-pgbouncer` 6432 → Pod 6432 | `PORT = 6432`, `targetPort` 동일 |
| PgBouncer → chat-db | 막히지 않음 | `cmp-cutover`를 고르는 정책 없음(송신 제한 없음), `chat-db`를 고르는 수신 정책 없음 |
| PgBouncer 수신 | 막히지 않음 | `cmp-cutover`를 고르는 수신 정책 없음 |

`chat-source`의 NetworkPolicy는 적용 전 `chat-app-egress` 하나뿐이었다(2026-11-07 확인).

## 적용

```bash
export AWS_PROFILE=cmp-aws-prod
KC=$(mktemp)   # 기본 kubeconfig를 건드리지 않는다
aws eks update-kubeconfig --name cmp-demo-source --region ap-northeast-2 --kubeconfig "$KC"
kubectl --kubeconfig "$KC" apply --dry-run=server -f infra/data-migration/source-prep/cmp-allow-chat-app-to-cutover.yaml
kubectl --kubeconfig "$KC" apply -f infra/data-migration/source-prep/cmp-allow-chat-app-to-cutover.yaml
```

적용은 chat-app을 재시작하지 않는다(허용만 늘어남).

## 되돌리기

```bash
kubectl --kubeconfig "$KC" -n chat-source delete networkpolicy cmp-allow-chat-app-to-cutover
```

이 정책만 지운다. `chat-app-egress` 등 원본 리소스는 지우지 않는다. 단, chat-app이 이미 PgBouncer로 바뀐 뒤 지우면 새로 뜨는 chat-app Pod가 DB에 못 붙는다.

## 적용 기록

결과는 [`tickets/data-migration/12-source-netpol-pgbouncer.md`](../../../tickets/data-migration/12-source-netpol-pgbouncer.md)에 적는다.
