import stat
from pathlib import Path

import pytest
from cmp_scanners.manifests import ManifestScanError, render_helm_chart, scan_manifests

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analysis" / "manifests"
SECRET_VALUES = ["c3VwZXItc2VjcmV0LXBhc3N3b3Jk", "plain-token-value-12345"]


@pytest.fixture(scope="module")
def scan():
    return scan_manifests(FIXTURES)


def kinds(items, field="kind"):
    return {getattr(i, field) for i in items}


def test_cloud_dependencies_are_detected_with_evidence(scan):
    deps = {(d.kind, d.resource): d for d in scan.cloud_dependencies}
    assert ("ALB_INGRESS", "Ingress/shop-api") in deps
    assert ("IRSA", "ServiceAccount/shop-api") in deps
    assert ("EBS_STORAGE_CLASS", "StorageClass/gp3") in deps
    assert deps[("IRSA", "ServiceAccount/shop-api")].evidence.startswith("app.yaml#")
    assert "eks.amazonaws.com/role-arn" in deps[("IRSA", "ServiceAccount/shop-api")].evidence


def test_ack_custom_resources_are_classified_as_aws_only_services(scan):
    dep = next(d for d in scan.cloud_dependencies if d.kind == "AWS_ONLY_SERVICE")
    assert dep.resource == "DBInstance/shop-rds"
    assert all(u.resource != "DBInstance/shop-rds" for u in scan.unconvertible)


def test_workload_requirements_come_with_evidence_paths(scan):
    api = next(w for w in scan.workloads if w.name == "shop-api")
    assert api.kind == "Deployment" and api.replicas == 3
    assert api.containers[0].ports == [8080]
    assert api.containers[0].resources.model_dump() == {
        "cpu_request": "250m",
        "cpu_limit": "500m",
        "memory_request": "256Mi",
        "memory_limit": "512Mi",
    }
    assert api.containers[0].readiness_probe == "http:/health:8080"
    assert api.containers[0].liveness_probe == "tcp:8080"
    assert api.evidence == "app.yaml#Deployment/shop-api"
    assert api.containers[0].evidence.endswith("/spec/template/spec/containers/0")


def test_postgres_images_are_recorded_as_information(scan):
    images = {p.resource: p for p in scan.postgres_images}
    pinned = images["StatefulSet/shop-db"]
    assert pinned.digest == "sha256:" + "0123456789abcdef" * 4
    assert pinned.needs_lookup is False
    tagged = images["Deployment/reporting-db"]
    assert tagged.digest is None and tagged.needs_lookup is True
    assert tagged.image == "bitnami/postgresql:16.2.0"
    assert "shop-api" not in {p.resource.split("/")[1] for p in scan.postgres_images}


def test_unconvertible_items_are_listed(scan):
    found = {(u.kind, u.resource) for u in scan.unconvertible}
    assert ("SIDECAR", "Deployment/shop-api") in found
    assert ("INIT_CONTAINER", "Deployment/shop-api") in found
    assert ("CRD", "CustomResourceDefinition/widgets.example.com") in found
    assert ("CUSTOM_RESOURCE", "Widget/w1") in found
    assert ("UNKNOWN_ANNOTATION", "Deployment/shop-api") in found
    details = {u.detail for u in scan.unconvertible}
    assert "log-shipper" in details and "wait-db" in details
    assert "example.com/owner" in details


def test_known_cloud_annotations_are_not_reported_as_unknown(scan):
    details = {u.detail for u in scan.unconvertible if u.kind == "UNKNOWN_ANNOTATION"}
    assert not any(d.startswith(("alb.ingress", "eks.amazonaws.com")) for d in details)


def test_secrets_keep_names_and_keys_only(scan):
    secret = next(s for s in scan.secrets if s.name == "shop-secrets")
    assert secret.keys == ["API_TOKEN", "DB_PASSWORD"]
    dumped = scan.model_dump_json()
    for value in SECRET_VALUES:
        assert value not in dumped


def test_references_link_workloads_to_config_secrets_and_volumes(scan):
    refs = {(r.kind, r.name) for r in scan.references if r.source == "Deployment/shop-api"}
    assert refs == {("Secret", "shop-secrets"), ("ConfigMap", "shop-config"), ("PVC", "shop-data")}


def test_unparseable_files_are_reported_not_fatal(scan):
    assert [u.path for u in scan.unparsed] == ["broken.yaml"]


def test_single_file_and_missing_path(tmp_path):
    only = scan_manifests(FIXTURES / "database.yaml")
    assert {w.name for w in only.workloads} == {"shop-db", "reporting-db"}
    with pytest.raises(FileNotFoundError):
        scan_manifests(tmp_path / "nope")


def test_helm_render_calls_helm_template_and_writes_output(tmp_path):
    fake = tmp_path / "helm"
    fake.write_text(
        '#!/bin/sh\necho "$@" > "$0.args"\n'
        "printf '%s\\n' 'apiVersion: v1' 'kind: ConfigMap' 'metadata:' '  name: from-helm'\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    chart = tmp_path / "chart"
    chart.mkdir()
    out = render_helm_chart(chart, release="rel", helm_bin=str(fake), output_dir=tmp_path / "out")
    assert (tmp_path / "helm.args").read_text().split()[:2] == ["template", "rel"]
    assert "from-helm" in (out / "rendered.yaml").read_text()
    assert scan_manifests(out).unparsed == []


def test_helm_failure_raises_without_dumping_output(tmp_path):
    fake = tmp_path / "helm"
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(ManifestScanError, match="boom"):
        render_helm_chart(tmp_path, release="r", helm_bin=str(fake), output_dir=tmp_path / "o")


def test_legacy_alb_class_and_exporter_images(tmp_path):
    (tmp_path / "x.yaml").write_text(
        "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: old\n"
        "  annotations:\n    kubernetes.io/ingress.class: alb\n---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: metrics\nspec:\n"
        "  template:\n    spec:\n      containers:\n"
        "        - name: e\n          image: prometheuscommunity/postgres-exporter:v0.15\n"
    )
    result = scan_manifests(tmp_path)
    assert [d.kind for d in result.cloud_dependencies] == ["ALB_INGRESS"]
    assert "ingress.class" in result.cloud_dependencies[0].evidence
    assert result.postgres_images == []


def test_postgres_identity_and_volume_claims_are_extracted_without_secrets():
    result = scan_manifests(Path(__file__).parent.parent / "fixtures/analysis/assembly/manifests")
    db = next(w for w in result.workloads if w.name == "shop-db")
    assert db.containers[0].env_literals == {"POSTGRES_DB": "shopdb", "POSTGRES_USER": "shopuser"}
    assert [(c.name, c.storage) for c in result.volume_claims] == [("pgdata", "20Gi")]
    assert ("PVC", "pgdata") in {
        (r.kind, r.name) for r in result.references if r.source == db.evidence.split("#")[1]
    }
    assert "POSTGRES_PASSWORD" not in db.containers[0].env_literals  # names only, never values


SOURCE_LIKE = Path(__file__).parent.parent / "fixtures/analysis/source-like"


def test_network_policy_egress_between_pods_becomes_a_network_rule():
    rules = scan_manifests(SOURCE_LIKE).network_rules
    assert [
        (r.policy, r.namespace, r.source_selector, r.destination_selector, r.port, r.protocol)
        for r in rules
    ] == [("orders-api-egress", "orders", {"app": "orders-api"}, {"app": "orders-db"}, 5432, "TCP")]
    assert rules[0].evidence.startswith("stack.yaml#NetworkPolicy/orders-api-egress")


def test_workloads_carry_pod_labels_and_declared_env_names_without_values():
    result = scan_manifests(SOURCE_LIKE)
    api = next(w for w in result.workloads if w.name == "orders-api")
    assert api.labels == {"app": "orders-api"}
    assert api.env_names == [
        "APP_PORT", "DB_HOST", "DB_NAME", "DB_PASSWORD", "DB_PORT", "DB_USER", "FEATURE_X", "WRITE_TOKEN",
    ]  # fmt: skip
    assert [(c.name, c.keys) for c in result.configmaps] == [
        ("orders-config", ["APP_PORT", "DB_HOST", "DB_NAME", "DB_PORT", "DB_USER", "FEATURE_X"])
    ]
    dumped = result.model_dump_json()
    assert "not-a-real-secret" not in dumped and "svc.cluster.local" not in dumped
