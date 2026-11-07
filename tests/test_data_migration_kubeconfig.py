"""infra/onprem-agent/make-data-kubeconfig.py (Provisioning 75, #304).

RBAC는 티켓 75의 권한 표(데이터 Activity가 대상 K3s에 하는 kubectl 호출)와 빠짐없이·초과 없이 같아야 하고,
kubeconfig는 `tls-server-name`을 쓰며 토큰은 0600 파일에만 남는다.
"""

import base64
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest
from cmp_data import tunnel as wg

SCRIPT = Path(__file__).parents[1] / "infra/onprem-agent/make-data-kubeconfig.py"
spec = importlib.util.spec_from_file_location("make_data_kubeconfig", SCRIPT)
mk = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mk  # dataclass가 모듈을 찾는다
spec.loader.exec_module(mk)

APP = "cmp-app"
TOKEN = "eyJhbGciOi.secret-token-value.sig"
CA = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n").decode()

# 티켓 75 권한 표. (범위, API 그룹, 리소스, 동사, resourceName). 범위 "app"=대상 DB Namespace,
# "tunnel"=cmp-data-tunnel, "cluster"=클러스터 범위.
APPLIED = ("get", "create", "patch", "delete")
WATCHED = (*APPLIED, "list", "watch")
DB_POD = {("app", "", "pods", v, None) for v in ("get", "list", "watch", "delete")}
DB_EXEC = {("app", "", "pods/exec", "create", None)}
DB_SECRET = {("app", "", "secrets", v, None) for v in ("get", "create")}
DB_POLICY = {("app", "networking.k8s.io", "networkpolicies", v, None) for v in APPLIED}
TUNNEL_APPLIED = (
    {("tunnel", "", "configmaps", v, None) for v in APPLIED}
    | {("tunnel", "", "services", v, None) for v in WATCHED}
    | {("tunnel", "apps", "deployments", v, None) for v in WATCHED}
)
TUNNEL_SECRET = {("tunnel", "", "secrets", v, None) for v in ("get", "create", "update", "delete")}
TUNNEL_POD = {("tunnel", "", "pods", v, None) for v in ("get", "list")}
TUNNEL_EXEC = {("tunnel", "", "pods/exec", "create", None)}
CLUSTER_NS = {("cluster", "", "namespaces", v, wg.NAMESPACE) for v in ("get", "patch")}
EXPECTED = (
    DB_POD | DB_EXEC | DB_SECRET | DB_POLICY
    | TUNNEL_APPLIED | TUNNEL_SECRET | TUNNEL_POD | TUNNEL_EXEC | CLUSTER_NS
)  # fmt: skip


def _flatten(manifests: list[dict]) -> set[tuple]:
    out = set()
    for m in manifests:
        if m["kind"] not in ("Role", "ClusterRole"):
            continue
        ns = m["metadata"].get("namespace")
        if m["metadata"]["name"] == mk.DB_ROLE:
            scope = "app"  # ClusterRole이지만 앱 Namespace의 RoleBinding으로만 효력이 있다
        elif m["kind"] == "ClusterRole":
            scope = "cluster"
        else:
            scope = "tunnel" if ns == wg.NAMESPACE else "app"
        for rule in m["rules"]:
            names = rule.get("resourceNames") or [None]
            for group in rule["apiGroups"]:
                for resource in rule["resources"]:
                    for verb in rule["verbs"]:
                        for name in names:
                            out.add((scope, group, resource, verb, name))
    return out


def test_rbac_matches_permission_table_exactly():
    assert _flatten(mk.rbac_manifests(APP)) == EXPECTED


def test_db_permissions_reach_only_namespaces_with_the_binding():
    """Provisioning 84: DB 권한은 ClusterRole에 있지만 ClusterRoleBinding은 없다."""
    manifests = mk.rbac_manifests(APP)
    [role] = [m for m in manifests if m["metadata"]["name"] == mk.DB_ROLE and "rules" in m]
    assert role["kind"] == "ClusterRole" and role["rules"] == mk.DB_RULES
    cluster_bindings = [m for m in manifests if m["kind"] == "ClusterRoleBinding"]
    assert all(b["roleRef"]["name"] != mk.DB_ROLE for b in cluster_bindings)
    [binding] = [m for m in manifests if m["kind"] == "RoleBinding"
                 and m["roleRef"]["name"] == mk.DB_ROLE]  # fmt: skip
    assert binding == mk.db_binding(APP)
    assert binding["metadata"]["namespace"] == APP


def test_agent_creates_the_same_binding_as_the_script():
    """Agent가 Namespace를 만들 때 만드는 연결과 이름·대상·roleRef가 같아야 한다(라벨만 다르다)."""
    from cmp_providers import onprem_agent

    script = mk.db_binding(APP)
    assert onprem_agent.DATA_MIGRATION_ROLE == mk.DB_ROLE
    assert [onprem_agent.DATA_MIGRATION_SUBJECT] == script["subjects"]

    applied = []

    class Target:
        def get(self, kind, name, namespace=None):
            return None

        def apply(self, document):
            applied.append(document)

    runner = onprem_agent.RolloutRunner(Target(), fetch=None)
    runner._bind_data_migration(APP)
    [agent] = applied
    assert agent["roleRef"] == script["roleRef"] and agent["subjects"] == script["subjects"]
    assert agent["metadata"]["name"] == script["metadata"]["name"]
    assert agent["metadata"]["namespace"] == APP


def test_rbac_has_no_wildcards_and_binds_only_the_service_account():
    manifests = mk.rbac_manifests(APP)
    assert "*" not in json.dumps(manifests)
    bindings = [m for m in manifests if m["kind"] in ("RoleBinding", "ClusterRoleBinding")]
    roles = {(m["kind"], m["metadata"].get("namespace"), m["metadata"]["name"]) for m in manifests
             if m["kind"] in ("Role", "ClusterRole")}  # fmt: skip
    assert len(bindings) == len(roles) == 3
    for b in bindings:
        assert b["subjects"] == [
            {"kind": "ServiceAccount", "name": mk.SA_NAME, "namespace": mk.SA_NAMESPACE}
        ]
        ref = b["roleRef"]
        assert (ref["kind"], b["metadata"].get("namespace") if ref["kind"] == "Role" else None,
                ref["name"]) in roles  # fmt: skip


def test_rbac_creates_service_account_and_tunnel_namespace_but_not_app_namespace():
    manifests = mk.rbac_manifests(APP)
    namespaces = {m["metadata"]["name"]: m for m in manifests if m["kind"] == "Namespace"}
    assert set(namespaces) == {mk.SA_NAMESPACE, wg.NAMESPACE}  # 앱 Namespace는 Agent 소유
    assert namespaces[wg.NAMESPACE]["metadata"]["labels"] == wg.LABELS  # 데이터 코드와 같은 라벨
    sa = [m for m in manifests if m["kind"] == "ServiceAccount"]
    assert sa == [mk.service_account()]
    assert sa[0]["automountServiceAccountToken"] is False
    # Namespace를 먼저 만들어야 그 안의 객체가 적용된다
    order = [m["kind"] for m in manifests]
    assert order.index("Namespace") < order.index("ServiceAccount") < order.index("Role")


def test_rbac_without_app_namespace_keeps_db_role_but_skips_binding():
    manifests = mk.rbac_manifests(None)
    assert _flatten(manifests) == EXPECTED  # ClusterRole은 Namespace가 없어도 만든다
    assert not [m for m in manifests if m["kind"] == "RoleBinding"
                and m["roleRef"]["name"] == mk.DB_ROLE]  # fmt: skip


@pytest.mark.parametrize("bad", ["Bad", "-x", "kube-system", "cmp-data-tunnel", "a" * 64])
def test_rejects_unusable_app_namespace(bad):
    with pytest.raises(ValueError):
        mk.rbac_manifests(bad)


def test_kubeconfig_uses_tls_server_name_and_ca_without_skip_verify():
    cfg = mk.kubeconfig("https://10.0.1.5:16443", CA, TOKEN, APP)
    cluster = cfg["clusters"][0]["cluster"]
    assert cluster == {
        "server": "https://10.0.1.5:16443",
        "certificate-authority-data": CA,
        "tls-server-name": "kubernetes.default.svc",
    }
    assert cfg["users"][0]["user"] == {"token": TOKEN}
    assert cfg["contexts"][0]["context"]["namespace"] == APP
    assert cfg["current-context"] == cfg["contexts"][0]["name"]
    assert "insecure-skip-tls-verify" not in json.dumps(cfg)


@pytest.mark.parametrize(
    "server", ["http://10.0.1.5:6443", "10.0.1.5:6443", "https://", "https://h:p", "https://h/x"]
)
def test_kubeconfig_rejects_bad_server(server):
    with pytest.raises(ValueError):
        mk.kubeconfig(server, CA, TOKEN, APP)


class FakeKubectl:
    """`kubectl` 호출을 기록하고 토큰 요청·CA 조회에 가짜 응답을 준다."""

    def __init__(self, *, namespaces=(APP,)):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.namespaces = set(namespaces)

    def __call__(self, args: list[str], stdin: bytes | None = None) -> bytes:
        self.calls.append((args, stdin))
        if args[:2] == ["create", "token"]:
            status = {"token": TOKEN, "expirationTimestamp": "2026-11-08T00:00:00Z"}
            return json.dumps({"status": status}).encode()
        if args[:3] == ["get", "configmap", "kube-root-ca.crt"]:
            return base64.b64decode(CA)
        if args[:2] == ["get", "namespace"]:
            return (args[2].encode() + b"\n") if args[2] in self.namespaces else b""
        return b""


def _run(tmp_path, fake, *extra):
    argv = ["apply", "--context", "k3d-cmp-local", "--namespace", APP,
            "--server", "https://10.0.1.5:16443", "--ref", "onprem-demo",
            "--out-dir", str(tmp_path), *extra]  # fmt: skip
    return mk.main(argv, kubectl=fake)


def test_apply_writes_private_kubeconfig_and_never_prints_token(tmp_path, capsys):
    fake = FakeKubectl()
    assert _run(tmp_path, fake, "--duration", "6h") == 0
    path = tmp_path / "onprem-demo.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = json.loads(path.read_text())  # JSON은 YAML이다
    assert cfg["users"][0]["user"]["token"] == TOKEN
    assert cfg["clusters"][0]["cluster"]["tls-server-name"] == "kubernetes.default.svc"
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "2026-11-08T00:00:00Z" in out.out  # 만료 시각만 알린다
    # 토큰은 유효 시간을 정해 SA에서 발급한다
    token_call = next(a for a, _ in fake.calls if a[:2] == ["create", "token"])
    assert token_call[2:] == [mk.SA_NAME, "-n", mk.SA_NAMESPACE, "--duration", "6h", "-o", "json"]
    # 모든 호출은 명시한 context로 간다(기본 context를 몰래 쓰지 않는다)
    assert all("--context" not in a for a, _ in fake.calls)  # context는 실행기가 붙인다
    applied = [json.loads(s) for a, s in fake.calls if a[:2] == ["apply", "--server-side"]]
    assert all("--force-conflicts" in a for a, _ in fake.calls if a[0] == "apply")
    assert applied and applied[0]["items"] == mk.rbac_manifests(APP)


def test_apply_tightens_existing_file_permissions(tmp_path):
    path = tmp_path / "onprem-demo.yaml"
    path.write_text("old")
    path.chmod(0o644)
    assert _run(tmp_path, FakeKubectl()) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert TOKEN in path.read_text()


def test_apply_without_app_namespace_succeeds_and_leaves_binding_to_the_agent(tmp_path, capsys):
    """Provisioning 84: 앱 Namespace가 아직 없어도 할 일이 끝난다. Agent가 Namespace를 만들 때 연결한다."""
    fake = FakeKubectl(namespaces=())
    assert _run(tmp_path, fake) == 0
    assert (tmp_path / "onprem-demo.yaml").exists()
    applied = [json.loads(s) for a, s in fake.calls if a[:2] == ["apply", "--server-side"]]
    assert applied[0]["items"] == mk.rbac_manifests(None)
    out = capsys.readouterr().out
    assert "APP_NAMESPACE_PENDING" in out and "Agent" in out


@pytest.mark.parametrize("ref", [".hidden", "a/b", "x y", ""])
def test_rejects_bad_ref(tmp_path, ref):
    with pytest.raises(SystemExit):
        mk.main(["apply", "--context", "c", "--namespace", APP, "--server", "https://h:1",
                 "--ref", ref, "--out-dir", str(tmp_path)], kubectl=FakeKubectl())  # fmt: skip


def test_context_is_required():
    with pytest.raises(SystemExit):
        mk.main(["bind", "--namespace", APP], kubectl=FakeKubectl())


def test_bind_applies_rbac_without_issuing_token(tmp_path):
    fake = FakeKubectl()
    assert mk.main(["bind", "--context", "c", "--namespace", APP], kubectl=fake) == 0
    assert not any(a[:2] == ["create", "token"] for a, _ in fake.calls)
    assert [a[:2] for a, _ in fake.calls if a[0] == "apply"] == [["apply", "--server-side"]]


class RoleBoundKubectl(FakeKubectl):
    """Provisioning 84 이전 `bind`가 앱 Namespace에 Role로 연결해 둔 클러스터."""

    def __call__(self, args, stdin=None):
        if args[:2] == ["get", "rolebinding"]:
            self.calls.append((args, stdin))
            return b"Role"
        return super().__call__(args, stdin)


def test_bind_replaces_an_old_namespaced_role_binding():
    fake = RoleBoundKubectl()
    assert mk.main(["bind", "--context", "c", "--namespace", APP], kubectl=fake) == 0
    steps = [a[:2] for a, _ in fake.calls if a[0] in ("delete", "apply")]
    assert steps == [["delete", "rolebinding,role"], ["apply", "--server-side"]]  # roleRef는 불변


def test_bind_keeps_a_cluster_role_binding():
    fake = FakeKubectl()
    assert mk.main(["bind", "--context", "c", "--namespace", APP], kubectl=fake) == 0
    assert not [a for a, _ in fake.calls if a[0] == "delete"]


def test_delete_removes_rbac_and_service_account_namespace_only():
    fake = FakeKubectl()
    assert mk.main(["delete", "--context", "c", "--namespace", APP], kubectl=fake) == 0
    deleted = [a for a, _ in fake.calls if a[0] == "delete"]
    flat = " ".join(" ".join(a) for a in deleted)
    assert f"namespace {mk.SA_NAMESPACE}" in flat
    assert f"namespace {wg.NAMESPACE}" not in flat  # 터널 자원은 데이터 정리가 지운다
    assert f"namespace {APP}" not in flat
    assert f"clusterrole {mk.DB_ROLE}" in flat  # Agent가 만든 연결도 효력을 잃는다
    assert all("--ignore-not-found" in a for a in deleted)


def test_verify_expected_permissions_cover_table():
    """verify가 묻는 허용 검사는 표 전체, 거부 검사는 표 밖 대표 동작이다."""
    allowed = {(c.namespace_scope, c.group, c.resource, c.verb, c.name) for c in mk.checks(APP)
               if c.expect}  # fmt: skip
    assert allowed == EXPECTED
    denied = [c for c in mk.checks(APP) if not c.expect]
    assert {("cluster", "", "namespaces", "create", None), ("app", "", "secrets", "list", None),
            ("other", "", "pods/exec", "create", None)} <= {
        (c.namespace_scope, c.group, c.resource, c.verb, c.name) for c in denied}  # fmt: skip


def test_verify_reports_mismatch(capsys):
    def fake(args, stdin=None):
        # 모든 질문에 yes → 거부돼야 할 검사가 실패
        return b"yes\n"

    assert mk.main(["verify", "--context", "c", "--namespace", APP], kubectl=fake) == 1
    assert "UNEXPECTED_ALLOW" in capsys.readouterr().out
