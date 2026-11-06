#!/usr/bin/env python3
"""온프레미스 K3s에 데이터 이전 worker용 최소 권한 ServiceAccount·kubeconfig를 만든다(Provisioning 75, #304).

ADR-007 2안: worker는 중계 서버 → Agent 터널 → `kubernetes.default.svc:443`으로 대상 K3s API에 붙어
데이터 Activity(`kubectl exec … psql`, 게이트웨이 배치 등)를 실행한다. 권한은 티켓 75 권한 표만큼이다.

사용자 맥에서 클러스터 관리자 context로 실행한다(표준 라이브러리만 쓴다, kubectl 필요).

    python3 infra/onprem-agent/make-data-kubeconfig.py apply --context k3d-cmp-local \\
        --namespace <대상 앱 Namespace> --server https://<중계 서버 사설 IP>:<포트> --ref <kubeconfig_ref>
    python3 infra/onprem-agent/make-data-kubeconfig.py bind   --context … --namespace … [--wait-namespace 900]
    python3 infra/onprem-agent/make-data-kubeconfig.py verify --context … --namespace …
    python3 infra/onprem-agent/make-data-kubeconfig.py delete --context … --namespace …

토큰은 화면·로그에 내지 않고 0600 파일(`<out-dir>/<ref>.yaml`)에만 쓴다. 서버로 복사는 따로 승인받아 한다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SA_NAMESPACE = "cmp-data-migration"
SA_NAME = "cmp-data-migration"
TUNNEL_NAMESPACE = "cmp-data-tunnel"  # cmp_data.tunnel.NAMESPACE
TUNNEL_LABELS = {"app.kubernetes.io/managed-by": "cmp-data-migration"}  # cmp_data.tunnel.LABELS
LABELS = {"app.kubernetes.io/managed-by": "cmp-data-kubeconfig"}
TLS_SERVER_NAME = "kubernetes.default.svc"  # k3s 인증서 SAN에 있다(터널·중계 서버 IP는 없다)
FIELD_MANAGER = "cmp-data-kubeconfig"
DB_ROLE = "cmp-data-migration-db"
TUNNEL_ROLE = "cmp-data-migration-tunnel"
CLUSTER_ROLE = "cmp-data-migration-namespace"
DEFAULT_OUT_DIR = Path.home() / ".config/cmp/data-migration"
RESERVED = {
    "default",
    "kube-system",
    "kube-public",
    "kube-node-lease",
    SA_NAMESPACE,
    TUNNEL_NAMESPACE,
}
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
REF = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")  # handoff.target_kubeconfig_ref, 점 시작 금지

# SSA 적용(get·create·patch)과 삭제. `delete --wait`·`wait`·`rollout status`가 지켜봐야 하는 객체(종료 유예가
# 있는 Pod, LoadBalancer finalizer가 있을 수 있는 Service, rollout 대기 Deployment)만 list·watch를 더한다.
# 즉시 사라지는 ConfigMap·Secret·NetworkPolicy는 list·watch 없이 `delete --wait`가 끝난다(k3d 실측).
APPLIED = ["get", "create", "patch", "delete"]
WATCHED = [*APPLIED, "list", "watch"]

# 티켓 75 권한 표. 대상 앱(DB) Namespace: data_migration(psql·pg_dump exec, Secret, restart),
# data_tunnel(DB 허용 NetworkPolicy 적용·삭제). `pods/exec`은 create만으로 된다(kubectl 1.37·k3s 1.36 실측).
DB_RULES = [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "delete"]},
    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create"]},
    {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": APPLIED},
]
# cmp-data-tunnel: data_tunnel 게이트웨이(ConfigMap·Deployment·Service 적용, 키 Secret, rollout 대기,
# wg show exec, 정리 delete --wait).
TUNNEL_RULES = [
    {"apiGroups": [""], "resources": ["configmaps"], "verbs": APPLIED},
    {"apiGroups": [""], "resources": ["services"], "verbs": WATCHED},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": WATCHED},
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "create", "update", "delete"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
]
# 클러스터 범위: data_tunnel이 `namespace_manifest()`를 SSA로 다시 적용한다. Namespace는 이 도구가 미리
# 만들어 두므로 create는 주지 않고 그 이름 하나만 허용한다.
CLUSTER_RULES = [
    {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get", "patch"],
     "resourceNames": [TUNNEL_NAMESPACE]},
]  # fmt: skip


def _check_namespace(namespace: str) -> None:
    if not DNS_LABEL.match(namespace) or namespace in RESERVED:
        raise ValueError("APP_NAMESPACE_INVALID")


def _meta(name: str, namespace: str | None = None, labels: dict | None = None) -> dict:
    meta: dict = {"name": name, "labels": dict(labels or LABELS)}
    if namespace:
        meta["namespace"] = namespace
    return meta


def service_account() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": _meta(SA_NAME, SA_NAMESPACE),
        "automountServiceAccountToken": False,
    }


def _subject() -> list[dict]:
    return [{"kind": "ServiceAccount", "name": SA_NAME, "namespace": SA_NAMESPACE}]


def _role(name: str, namespace: str, rules: list[dict]) -> list[dict]:
    rbac = "rbac.authorization.k8s.io/v1"
    return [
        {"apiVersion": rbac, "kind": "Role", "metadata": _meta(name, namespace), "rules": rules},
        {
            "apiVersion": rbac,
            "kind": "RoleBinding",
            "metadata": _meta(name, namespace),
            "subjects": _subject(),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
        },
    ]


def rbac_manifests(app_namespace: str | None) -> list[dict]:
    """적용 순서대로. 앱 Namespace는 Agent 소유라 만들지 않는다(없으면 None으로 그 Role만 뺀다)."""
    if app_namespace is not None:
        _check_namespace(app_namespace)
    rbac = "rbac.authorization.k8s.io/v1"
    out = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(SA_NAMESPACE)},
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": _meta(TUNNEL_NAMESPACE, labels=TUNNEL_LABELS)},
        service_account(),
        {"apiVersion": rbac, "kind": "ClusterRole", "metadata": _meta(CLUSTER_ROLE),
         "rules": CLUSTER_RULES},
        {"apiVersion": rbac, "kind": "ClusterRoleBinding", "metadata": _meta(CLUSTER_ROLE),
         "subjects": _subject(),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                     "name": CLUSTER_ROLE}},
        *_role(TUNNEL_ROLE, TUNNEL_NAMESPACE, TUNNEL_RULES),
    ]  # fmt: skip
    if app_namespace is not None:
        out += _role(DB_ROLE, app_namespace, DB_RULES)
    return out


def kubeconfig(server: str, ca_b64: str, token: str, namespace: str) -> dict:
    parts = urlsplit(server)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "https" or not parts.hostname or not port or parts.path not in ("", "/"):
        raise ValueError("SERVER_INVALID")
    name = "cmp-data-migration"
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "cmp-onprem", "cluster": {
            "server": server.rstrip("/"),
            "certificate-authority-data": ca_b64,
            "tls-server-name": TLS_SERVER_NAME,
        }}],
        "users": [{"name": name, "user": {"token": token}}],
        "contexts": [{"name": name,
                      "context": {"cluster": "cmp-onprem", "user": name, "namespace": namespace}}],
        "current-context": name,
    }  # fmt: skip


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # 이미 있던 파일도 좁힌다
        os.write(fd, text.encode())
    finally:
        os.close(fd)


# ---- 확인(impersonation `auth can-i`) ----


@dataclass(frozen=True)
class Check:
    namespace_scope: str  # app | tunnel | cluster | other
    group: str
    resource: str
    verb: str
    name: str | None
    expect: bool


def _expand(scope: str, rules: list[dict]) -> list[Check]:
    return [
        Check(scope, g, r, v, n, True)
        for rule in rules
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
        for n in rule.get("resourceNames") or [None]
    ]


DENIED = [
    Check("cluster", "", "namespaces", "create", None, False),
    Check("cluster", "", "namespaces", "delete", TUNNEL_NAMESPACE, False),
    Check("cluster", "", "nodes", "get", None, False),
    Check("cluster", "rbac.authorization.k8s.io", "clusterroles", "create", None, False),
    Check("app", "", "secrets", "list", None, False),
    Check("app", "", "pods/exec", "get", None, False),
    Check("tunnel", "", "secrets", "list", None, False),
    Check("app", "apps", "deployments", "delete", None, False),
    Check("app", "", "services", "create", None, False),
    Check("tunnel", "", "pods", "delete", None, False),
    Check("tunnel", "rbac.authorization.k8s.io", "roles", "create", None, False),
    Check("other", "", "pods/exec", "create", None, False),
    Check("other", "", "secrets", "get", None, False),
]


def checks(app_namespace: str) -> list[Check]:
    return [*_expand("app", DB_RULES), *_expand("tunnel", TUNNEL_RULES),
            *_expand("cluster", CLUSTER_RULES), *DENIED]  # fmt: skip


def _can_i_args(check: Check, app_namespace: str) -> list[str]:
    resource, _, sub = check.resource.partition("/")
    target = f"{resource}.{check.group}" if check.group else resource
    if check.name:
        target += f"/{check.name}"
    args = ["auth", "can-i", check.verb, target]
    if sub:
        args.append(f"--subresource={sub}")
    namespace = {"app": app_namespace, "tunnel": TUNNEL_NAMESPACE, "other": "kube-system"}.get(
        check.namespace_scope
    )
    if namespace:
        args += ["-n", namespace]
    return [*args, f"--as=system:serviceaccount:{SA_NAMESPACE}:{SA_NAME}"]


# ---- kubectl ----

Kubectl = Callable[..., bytes]


class KubectlError(RuntimeError):
    pass


def real_kubectl(context: str) -> Kubectl:
    def run(args: list[str], stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["kubectl", "--context", context, *args],
            input=stdin,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if args[:2] == ["auth", "can-i"]:
            return proc.stdout  # "no"는 종료 코드 1이다
        if proc.returncode != 0:
            # 출력에는 토큰이 있을 수 있어 내지 않는다(create token). 동사만 알린다.
            raise KubectlError(f"KUBECTL_FAILED {' '.join(args[:2])}")
        return proc.stdout

    return run


def _apply(kubectl: Kubectl, manifests: list[dict]) -> None:
    body = json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests}).encode()
    # 이 도구가 소유한 객체다. 사람이 손으로 고친 필드도 표대로 되돌린다.
    kubectl(["apply", "--server-side", "--force-conflicts", "--field-manager", FIELD_MANAGER,
             "-f", "-"], body)  # fmt: skip


def _namespace_exists(kubectl: Kubectl, namespace: str) -> bool:
    out = kubectl(["get", "namespace", namespace, "--ignore-not-found", "-o", "name"])
    return bool(out.strip())


def _bind(kubectl: Kubectl, namespace: str, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while not _namespace_exists(kubectl, namespace):
        if time.monotonic() >= deadline:
            _apply(kubectl, rbac_manifests(None))
            return False
        time.sleep(5)
    _apply(kubectl, rbac_manifests(namespace))
    return True


def _root_ca(kubectl: Kubectl) -> str:
    for _ in range(30):  # kube-root-ca.crt는 Namespace를 만든 뒤 컨트롤러가 채운다
        pem = kubectl(["get", "configmap", "kube-root-ca.crt", "-n", SA_NAMESPACE,
                       "--ignore-not-found", "-o", r"jsonpath={.data.ca\.crt}"])  # fmt: skip
        if pem.strip():
            return base64.b64encode(pem).decode()
        time.sleep(1)
    raise KubectlError("ROOT_CA_NOT_FOUND")


def _pending(namespace: str) -> None:
    print(f"APP_NAMESPACE_PENDING {namespace}: 배포 뒤 `bind --namespace {namespace}`를 다시 실행한다"
          " (데이터 Activity는 권한 거부를 재시도한다)")  # fmt: skip


def cmd_apply(args, kubectl: Kubectl) -> int:
    kubeconfig(args.server, "x", "x", args.namespace)  # 클러스터를 바꾸기 전에 주소를 검증한다
    bound = _bind(kubectl, args.namespace, 0)
    raw = kubectl(["create", "token", SA_NAME, "-n", SA_NAMESPACE, "--duration", args.duration,
                   "-o", "json"])  # fmt: skip
    status = json.loads(raw)["status"]
    cfg = kubeconfig(args.server, _root_ca(kubectl), status["token"], args.namespace)
    path = Path(args.out_dir) / f"{args.ref}.yaml"
    write_private(path, json.dumps(cfg, indent=2) + "\n")
    print(f"KUBECONFIG_WRITTEN {path} (0600) expires {status.get('expirationTimestamp')}")
    if not bound:
        _pending(args.namespace)
        return 3
    return 0


def cmd_bind(args, kubectl: Kubectl) -> int:
    if _bind(kubectl, args.namespace, args.wait_namespace):
        print(f"RBAC_APPLIED {args.namespace}")
        return 0
    _pending(args.namespace)
    return 3


def cmd_verify(args, kubectl: Kubectl) -> int:
    bad = 0
    for check in checks(args.namespace):
        answer = kubectl(_can_i_args(check, args.namespace)).decode().strip()
        if (answer == "yes") != check.expect:
            bad += 1
            code = "MISSING" if check.expect else "UNEXPECTED_ALLOW"
            scope = check.namespace_scope
            print(f"{code} {scope} {check.verb} {check.group or 'core'}/{check.resource}"
                  + (f"/{check.name}" if check.name else ""))  # fmt: skip
    total = len(checks(args.namespace))
    print(f"VERIFY {'OK' if not bad else 'FAILED'} {total - bad}/{total}")
    return 1 if bad else 0


def cmd_delete(args, kubectl: Kubectl) -> int:
    """RBAC와 SA Namespace(토큰도 무효)를 지운다. cmp-data-tunnel은 데이터 정리가 맡아 남긴다."""
    gone = "--ignore-not-found"
    kubectl(["delete", "rolebinding,role", DB_ROLE, "-n", args.namespace, gone])
    kubectl(["delete", "rolebinding,role", TUNNEL_ROLE, "-n", TUNNEL_NAMESPACE, gone])
    kubectl(["delete", "clusterrolebinding,clusterrole", CLUSTER_ROLE, gone])
    kubectl(["delete", "namespace", SA_NAMESPACE, gone])
    print("RBAC_DELETED (cmp-data-tunnel Namespace는 남긴다)")
    return 0


def _ref(value: str) -> str:
    if not REF.match(value):
        raise argparse.ArgumentTypeError("ref must match [A-Za-z0-9._-]+ and not start with '.'")
    return value


def _app_namespace(value: str) -> str:
    try:
        _check_namespace(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--context", required=True, help="클러스터 관리자 kube context")
    common.add_argument("--namespace", required=True, type=_app_namespace,
                        help="대상 앱(DB) Namespace")  # fmt: skip
    a = sub.add_parser("apply", parents=[common], help="RBAC 적용 + 토큰 + kubeconfig")
    a.add_argument("--server", required=True, help="https://<중계 서버 주소>:<포트>")
    a.add_argument("--ref", required=True, type=_ref, help="target_kubeconfig_ref(파일 이름)")
    a.add_argument("--duration", default="12h", help="토큰 유효 시간(kubectl create token)")
    a.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    b = sub.add_parser("bind", parents=[common], help="RBAC만 적용(앱 Namespace 생긴 뒤)")
    b.add_argument("--wait-namespace", type=float, default=0, help="앱 Namespace를 기다릴 초")
    sub.add_parser("verify", parents=[common], help="SA 권한이 표와 같은지 auth can-i로 확인")
    sub.add_parser("delete", parents=[common], help="RBAC·SA 삭제(토큰 무효)")
    return p


def main(argv: list[str] | None = None, *, kubectl: Kubectl | None = None) -> int:
    args = parser().parse_args(argv)
    run = kubectl or real_kubectl(args.context)
    handler = {"apply": cmd_apply, "bind": cmd_bind, "verify": cmd_verify, "delete": cmd_delete}
    try:
        return handler[args.cmd](args, run)
    except (KubectlError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
