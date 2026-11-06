"""Ticket 28-B: the signals the manifest and code scans read for the security measure.

Ingress (paths, TLS), Ingress NetworkPolicies (who may reach a pod), and the `sslmode` a database
connection asks for. Only the mode word is kept, never the connection string around it, and the
scans never read `.env` values (AV-020).

Seam: the scanners on small files under `tmp_path`.
"""

from cmp_scanners.code import scan_code
from cmp_scanners.go_adapter import GoAdapter
from cmp_scanners.manifests import scan_manifests

PASSWORD = "s3cret-pw-value"


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return tmp_path


# --- Ingress ----------------------------------------------------------------------------------


def test_an_ingress_keeps_its_paths_and_whether_tls_is_configured(tmp_path):
    write(
        tmp_path,
        "ingress.yaml",
        """\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: chat
  annotations:
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:1:certificate/x
spec:
  ingressClassName: alb
  rules:
    - http:
        paths:
          - {path: /api, pathType: Prefix, backend: {service: {name: api, port: {number: 80}}}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: plain, namespace: chat}
spec:
  rules:
    - http:
        paths:
          - {pathType: Prefix, backend: {service: {name: api, port: {number: 80}}}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: tls-block, namespace: chat}
spec:
  tls: [{hosts: [chat.example.com], secretName: chat-tls}]
  rules:
    - host: chat.example.com
      http:
        paths:
          - {path: /, pathType: Prefix, backend: {service: {name: api, port: {number: 80}}}}
""",
    )
    scan = scan_manifests(tmp_path)
    by_name = {i.name: i for i in scan.ingresses}
    assert by_name["web"].tls is True and by_name["web"].paths == ["/api"]
    assert by_name["web"].namespace == "chat"
    assert by_name["web"].evidence == "ingress.yaml#Ingress/web"
    assert by_name["plain"].tls is False and by_name["plain"].paths == ["/"]
    assert by_name["tls-block"].tls is True


# --- Ingress NetworkPolicy --------------------------------------------------------------------


def policy(name, body):
    return f"""\
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {{name: {name}, namespace: chat}}
spec:
{body}
---
"""


def test_ingress_network_policies_record_who_may_reach_the_selected_pods(tmp_path):
    text = (
        policy(
            "db-from-api",
            """\
  podSelector: {matchLabels: {app: db}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: api}}}]
      ports: [{port: 5432}]""",
        )
        + policy(
            "open-by-omission",
            """\
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress:
    - ports: [{port: 80}]""",
        )
        + policy(
            "open-by-cidr",
            """\
  podSelector: {matchLabels: {app: cache}}
  policyTypes: [Ingress]
  ingress:
    - from: [{ipBlock: {cidr: 0.0.0.0/0}}]""",
        )
        + policy(
            "deny-all",
            """\
  podSelector: {matchLabels: {app: batch}}
  policyTypes: [Ingress]
  ingress: []""",
        )
        + policy(
            "egress-only",
            """\
  podSelector: {matchLabels: {app: api}}
  policyTypes: [Egress]
  egress:
    - to: [{podSelector: {matchLabels: {app: db}}}]
      ports: [{port: 5432}]""",
        )
    )
    write(tmp_path, "policies.yaml", text)
    scan = scan_manifests(tmp_path)
    by_name = {p.policy: p for p in scan.ingress_policies}
    assert set(by_name) == {"db-from-api", "open-by-omission", "open-by-cidr", "deny-all"}
    db = by_name["db-from-api"]
    assert db.target_selector == {"app": "db"} and db.source_selectors == [{"app": "api"}]
    assert db.allow_all is False and db.namespace == "chat"
    assert db.evidence == "policies.yaml#NetworkPolicy/db-from-api"
    assert by_name["open-by-omission"].allow_all is True
    assert by_name["open-by-cidr"].allow_all is True
    assert by_name["deny-all"].allow_all is False and by_name["deny-all"].source_selectors == []


def test_a_policy_without_policy_types_applies_to_ingress(tmp_path):
    write(
        tmp_path,
        "p.yaml",
        policy(
            "implicit",
            """\
  podSelector: {matchLabels: {app: db}}
  ingress:
    - from: [{podSelector: {matchLabels: {app: api}}}]""",
        ),
    )
    (only,) = scan_manifests(tmp_path).ingress_policies
    assert only.policy == "implicit" and only.source_selectors == [{"app": "api"}]


# --- sslmode in manifests ---------------------------------------------------------------------


def test_manifest_sslmode_is_read_as_a_mode_word_and_nothing_around_it(tmp_path):
    write(
        tmp_path,
        "app.yaml",
        f"""\
apiVersion: v1
kind: ConfigMap
metadata: {{name: app-config, namespace: chat}}
data:
  DATABASE_URL: postgres://app:{PASSWORD}@db:5432/app?sslmode=disable
  LOG_LEVEL: info
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: api, namespace: chat}}
spec:
  template:
    spec:
      containers:
        - name: api
          image: shop/api:1
          env:
            - {{name: DB_SSLMODE, value: require}}
            - {{name: PGPASSWORD, value: {PASSWORD}}}
            - {{name: SSLMODE, value: not-a-mode}}
""",
    )
    scan = scan_manifests(tmp_path)
    found = {(f.mode, f.evidence) for f in scan.ssl_modes}
    assert found == {
        ("disable", "app.yaml#ConfigMap/app-config/data/DATABASE_URL"),
        ("require", "app.yaml#Deployment/api/spec/template/spec/containers/0/env/DB_SSLMODE"),
    }
    assert PASSWORD not in scan.model_dump_json()


# --- sslmode in source code ---------------------------------------------------------------------


def test_source_code_sslmode_is_found_in_any_language_with_its_line(tmp_path):
    (tmp_path / "db.go").write_text(
        'package main\n\nconst dsn = "host=db user=app sslmode=disable"\n'
    )
    (tmp_path / "conn.py").write_text(
        'import psycopg2\nconn = psycopg2.connect(sslmode="require")\n'
    )
    (tmp_path / "notes.txt").write_text("sslmode=disable in a text note is not source code\n")
    code = scan_code(tmp_path, [], [GoAdapter()])
    assert {(s.mode, s.file, s.line) for s in code.ssl_modes} == {
        ("disable", "db.go", 3),
        ("require", "conn.py", 2),
    }


def test_env_files_are_not_read_for_sslmode(tmp_path):
    (tmp_path / ".env").write_text(f"DATABASE_URL=postgres://a:{PASSWORD}@db/x?sslmode=disable\n")
    (tmp_path / "main.go").write_text("package main\n")
    code = scan_code(tmp_path, ["DATABASE_URL"], [GoAdapter()])
    assert code.ssl_modes == []
    assert PASSWORD not in code.model_dump_json()
