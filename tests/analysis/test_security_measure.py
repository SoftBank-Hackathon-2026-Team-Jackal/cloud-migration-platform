"""Ticket 28-B: the source's security state, measured from what the scans already read.

Seam: the pure `measure_security(manifests, infra, code, routes)` on small objects, one criterion at
a time, plus the pipeline step that puts the measures in the report. Each case states its input and
the state it must give; nothing is read from the code under test. Without evidence the state is
UNKNOWN and never PROTECTED.
"""

from cmp_contracts.analysis import AnalysisInput, SecurityMeasure
from cmp_scanners.aws_infra import (
    AwsInfraScan,
    CloudFrontInfo,
    EksCluster,
    ListenerInfo,
    LoadBalancerInfo,
    SecurityGroupInfo,
    SecurityGroupRule,
)
from cmp_scanners.code import CodeScan, EnvComparison, SslModeSite
from cmp_scanners.manifests import (
    IngressInfo,
    IngressPolicy,
    ManifestScan,
    PostgresImage,
    SslModeFinding,
    Workload,
)
from cmp_scanners.routes import HttpRoute
from cmp_scanners.security import measure_security
from cmp_worker.artifact_store import ArtifactRef
from pipeline_support import MAIN_GO, git, make

CRITERIA = ["EXTERNAL_HTTPS", "DB_ACCESS_SCOPE", "DB_TRANSPORT_ENCRYPTION", "ADMIN_API_EXPOSURE"]


# --- builders ---------------------------------------------------------------------------------


def manifests(**kw) -> ManifestScan:
    empty = {
        "workloads": [],
        "cloud_dependencies": [],
        "postgres_images": [],
        "unconvertible": [],
        "secrets": [],
        "references": [],
        "volume_claims": [],
        "configmaps": [],
        "network_rules": [],
        "unparsed": [],
    }
    return ManifestScan(**{**empty, **kw})


def code(*sites: SslModeSite) -> CodeScan:
    return CodeScan(
        call_sites=[],
        unscannable=[],
        extension_creations=[],
        env_comparison=EnvComparison(only_in_env=[], missing_from_env=[], partial=False),
        ssl_modes=list(sites),
    )


def listener(port, protocol, *, redirect=False, cert=False):
    return ListenerInfo(
        port=port,
        protocol=protocol,
        target_ports=[8080],
        has_certificate=cert,
        redirects_to_https=redirect,
    )


def balancer(name="shop", scheme="internet-facing", *listeners):
    return LoadBalancerInfo(
        name=name,
        type="application",
        scheme=scheme,
        listeners=list(listeners),
        dns_name=f"{name}.x",
    )


def infra(*, balancers=(), distributions=(), groups=(), status="COLLECTED") -> AwsInfraScan:
    return AwsInfraScan(
        cluster=EksCluster(name="c", version=None, vpc_id="vpc-1", security_group_ids=[]),
        vpcs=[],
        subnets=[],
        route_tables=[],
        security_groups=list(groups),
        load_balancers=list(balancers),
        role_policies=[],
        cloudtrail=[],
        duration_seconds=0.1,
        cloudfront=list(distributions),
        cloudfront_status=status,
    )


def distribution(dist_id, *policies):
    return CloudFrontInfo(
        id=dist_id,
        domain_name=f"{dist_id.lower()}.cloudfront.net",
        aliases=[],
        enabled=True,
        viewer_protocol_policies=sorted(policies),
        origin_domains=["shop.x"],
    )


def ingress(name="web", *, tls=False, paths=("/",)):
    return IngressInfo(
        name=name,
        namespace="chat",
        tls=tls,
        paths=list(paths),
        evidence=f"ingress.yaml#Ingress/{name}",
    )


def database(name="shop-db", labels=None):
    return (
        PostgresImage(
            resource=f"StatefulSet/{name}",
            image="postgres:16",
            digest=None,
            needs_lookup=True,
            evidence=f"db.yaml#StatefulSet/{name}/spec/template/spec/containers/0",
        ),
        Workload(
            kind="StatefulSet",
            name=name,
            namespace="chat",
            replicas=1,
            containers=[],
            evidence=f"db.yaml#StatefulSet/{name}",
            labels=labels if labels is not None else {"app": "db"},
        ),
    )


def policy(name, *, target=None, sources=(), allow_all=False):
    return IngressPolicy(
        policy=name,
        namespace="chat",
        target_selector=target if target is not None else {"app": "db"},
        source_selectors=list(sources),
        allow_all=allow_all,
        evidence=f"policies.yaml#NetworkPolicy/{name}",
    )


def sg(sg_id, *rules):
    return SecurityGroupInfo(
        id=sg_id, name=sg_id, vpc_id="vpc-1", tags={}, ingress=list(rules), egress=[]
    )


def sg_rule(port, *, cidrs=(), protocol="tcp"):
    return SecurityGroupRule(
        protocol=protocol, from_port=port, to_port=port, cidrs=list(cidrs), source_group_ids=[]
    )


def route(path, method="GET", file="main.go", line=10):
    return HttpRoute(method=method, path=path, file=file, line=line)


def measure(criterion, *, m=None, i=None, c=None, routes=()) -> SecurityMeasure:
    result = measure_security(m, i, c, list(routes))
    assert [x.criterion for x in result] == CRITERIA  # always all four, in this order
    return next(x for x in result if x.criterion == criterion)


# --- external HTTPS ---------------------------------------------------------------------------


def test_https_everywhere_is_protected():
    result = measure(
        "EXTERNAL_HTTPS",
        i=infra(
            balancers=[balancer("shop", "internet-facing", listener(443, "HTTPS", cert=True))],
            distributions=[distribution("EONE", "redirect-to-https")],
        ),
    )
    assert result.state == "PROTECTED"
    assert {e.locator for e in result.evidence} == {"elbv2:shop:443", "cloudfront:EONE"}


def test_a_plain_http_listener_without_a_redirect_is_unprotected():
    result = measure(
        "EXTERNAL_HTTPS",
        i=infra(balancers=[balancer("shop", "internet-facing", listener(80, "HTTP"))]),
    )
    assert result.state == "UNPROTECTED"
    assert result.reasons == ["LISTENER_HTTP_NO_REDIRECT shop:80"]


def test_an_http_listener_that_redirects_to_https_is_protected():
    result = measure(
        "EXTERNAL_HTTPS",
        i=infra(
            balancers=[
                balancer(
                    "shop",
                    "internet-facing",
                    listener(80, "HTTP", redirect=True),
                    listener(443, "HTTPS", cert=True),
                )
            ]
        ),
    )
    assert result.state == "PROTECTED"


def test_cloudfront_that_allows_http_makes_the_front_door_partial():
    result = measure(
        "EXTERNAL_HTTPS",
        i=infra(
            balancers=[balancer("shop", "internet-facing", listener(443, "HTTPS", cert=True))],
            distributions=[distribution("EONE", "allow-all", "redirect-to-https")],
        ),
    )
    assert result.state == "PARTIAL"
    assert "CLOUDFRONT_ALLOWS_HTTP EONE" in result.reasons


def test_an_internal_load_balancer_is_not_an_external_entry():
    result = measure(
        "EXTERNAL_HTTPS", i=infra(balancers=[balancer("inner", "internal", listener(80, "HTTP"))])
    )
    assert result.state == "UNKNOWN" and result.reasons == ["NO_EXTERNAL_ENTRY_VISIBLE"]


def test_the_ingress_is_used_when_the_aws_scan_shows_no_entry():
    secure = measure("EXTERNAL_HTTPS", m=manifests(ingresses=[ingress(tls=True)]))
    plain = measure("EXTERNAL_HTTPS", m=manifests(ingresses=[ingress(tls=False)]))
    assert secure.state == "PROTECTED" and plain.state == "UNPROTECTED"
    assert plain.reasons == ["INGRESS_NO_TLS web"]
    assert plain.evidence[0].kind == "MANIFEST"


def test_nothing_visible_is_unknown_and_a_denied_cloudfront_read_is_said_so():
    assert measure("EXTERNAL_HTTPS").state == "UNKNOWN"
    result = measure(
        "EXTERNAL_HTTPS",
        i=infra(
            balancers=[balancer("shop", "internet-facing", listener(443, "HTTPS", cert=True))],
            status="DENIED",
        ),
    )
    assert result.state == "PROTECTED" and "CLOUDFRONT_NOT_READ" in result.reasons


# --- DB access scope --------------------------------------------------------------------------


def db_manifests(*policies):
    postgres, workload = database()
    return manifests(
        postgres_images=[postgres], workloads=[workload], ingress_policies=list(policies)
    )


def test_a_database_that_only_its_app_may_reach_is_protected():
    result = measure(
        "DB_ACCESS_SCOPE", m=db_manifests(policy("db-from-api", sources=[{"app": "api"}]))
    )
    assert result.state == "PROTECTED"
    assert result.evidence[0].locator == "policies.yaml#NetworkPolicy/db-from-api"


def test_a_database_without_an_ingress_policy_is_reachable_by_every_pod():
    result = measure("DB_ACCESS_SCOPE", m=db_manifests())
    assert result.state == "UNPROTECTED" and result.reasons == ["NO_INGRESS_NETWORK_POLICY shop-db"]


def test_a_policy_that_allows_every_source_does_not_protect():
    result = measure("DB_ACCESS_SCOPE", m=db_manifests(policy("open", allow_all=True)))
    assert result.state == "UNPROTECTED" and result.reasons == ["NETWORK_POLICY_ALLOWS_ALL shop-db"]


def test_a_policy_for_other_pods_does_not_count():
    other = policy("web-only", target={"app": "web"}, sources=[{"app": "lb"}])
    assert measure("DB_ACCESS_SCOPE", m=db_manifests(other)).state == "UNPROTECTED"


def test_a_database_port_open_to_the_internet_in_a_security_group_is_unprotected():
    result = measure(
        "DB_ACCESS_SCOPE",
        m=db_manifests(policy("db-from-api", sources=[{"app": "api"}])),
        i=infra(groups=[sg("sg-db", sg_rule(5432, cidrs=["0.0.0.0/0"]))]),
    )
    assert result.state == "PARTIAL"
    assert "SG_DB_PORT_OPEN_TO_INTERNET sg-db" in result.reasons
    only_sg = measure(
        "DB_ACCESS_SCOPE", i=infra(groups=[sg("sg-db", sg_rule(5432, cidrs=["0.0.0.0/0"]))])
    )
    assert only_sg.state == "UNPROTECTED"


def test_no_database_in_sight_is_unknown():
    result = measure(
        "DB_ACCESS_SCOPE", m=manifests(), i=infra(groups=[sg("sg-app", sg_rule(8080))])
    )
    assert result.state == "UNKNOWN" and result.reasons == ["DB_NOT_FOUND_IN_MANIFESTS"]


# --- DB transport encryption ------------------------------------------------------------------


def test_plaintext_modes_are_unprotected_and_encrypted_modes_are_protected():
    weak = measure(
        "DB_TRANSPORT_ENCRYPTION", c=code(SslModeSite(mode="disable", file="db.go", line=3))
    )
    strong = measure(
        "DB_TRANSPORT_ENCRYPTION",
        m=manifests(ssl_modes=[SslModeFinding(mode="verify-full", evidence="a.yaml#C/x")]),
    )
    assert weak.state == "UNPROTECTED" and weak.reasons == ["SSLMODE_PLAINTEXT disable"]
    assert (weak.evidence[0].kind, weak.evidence[0].locator) == ("CODE", "db.go:3")
    assert strong.state == "PROTECTED" and strong.evidence[0].kind == "MANIFEST"


def test_prefer_can_fall_back_to_plaintext_so_it_is_not_protection():
    result = measure(
        "DB_TRANSPORT_ENCRYPTION", c=code(SslModeSite(mode="prefer", file="db.go", line=1))
    )
    assert result.state == "UNPROTECTED"


def test_mixed_modes_are_partial():
    result = measure(
        "DB_TRANSPORT_ENCRYPTION",
        c=code(
            SslModeSite(mode="require", file="a.go", line=1),
            SslModeSite(mode="disable", file="b.go", line=2),
        ),
    )
    assert result.state == "PARTIAL"


def test_a_mode_that_is_not_visible_is_unknown_not_protected():
    result = measure("DB_TRANSPORT_ENCRYPTION", m=manifests(), c=code())
    assert result.state == "UNKNOWN" and result.reasons == ["SSLMODE_NOT_VISIBLE"]


# --- admin API exposure -----------------------------------------------------------------------


def test_an_admin_looking_route_behind_an_open_entry_is_unprotected():
    result = measure(
        "ADMIN_API_EXPOSURE",
        m=manifests(ingresses=[ingress(paths=("/",))]),
        routes=[route("/api/messages"), route("/admin/users", "POST", "admin.go", 12)],
    )
    assert result.state == "UNPROTECTED"
    assert "ADMIN_PATH_CANDIDATE POST /admin/users" in result.reasons
    assert "ADMIN_PATH_UNCONFIRMED" in result.reasons  # a pattern match is a candidate only
    assert [(e.kind, e.locator) for e in result.evidence] == [("CODE", "admin.go:12")]


def test_an_ingress_that_only_routes_other_paths_protects_the_admin_route():
    result = measure(
        "ADMIN_API_EXPOSURE",
        m=manifests(ingresses=[ingress(paths=("/api",))]),
        routes=[route("/api/messages"), route("/admin/users")],
    )
    assert result.state == "PROTECTED"


def test_routes_without_admin_looking_paths_are_not_applicable():
    result = measure("ADMIN_API_EXPOSURE", routes=[route("/api/messages"), route("/health")])
    assert result.state == "NOT_APPLICABLE"


def test_candidates_with_no_way_to_tell_if_the_app_is_reachable_are_unknown():
    result = measure("ADMIN_API_EXPOSURE", routes=[route("/debug/pprof/heap")])
    assert result.state == "UNKNOWN" and "EXTERNAL_REACHABILITY_UNKNOWN" in result.reasons


def test_no_routes_at_all_is_unknown():
    result = measure("ADMIN_API_EXPOSURE", m=manifests(ingresses=[ingress()]), routes=[])
    assert result.state == "UNKNOWN" and result.reasons == ["NO_ROUTES_VISIBLE"]


def test_the_candidate_list_is_conventions_not_one_apps_paths():
    paths = ["/admin", "/internal/jobs", "/debug/vars", "/pprof/", "/actuator/env", "/metrics"]
    result = measure(
        "ADMIN_API_EXPOSURE",
        m=manifests(ingresses=[ingress(paths=("/",))]),
        routes=[route(p) for p in paths],
    )
    assert len([r for r in result.reasons if r.startswith("ADMIN_PATH_CANDIDATE")]) == len(paths)


# --- general ----------------------------------------------------------------------------------


def test_the_same_input_gives_the_same_measures():
    m = db_manifests()
    i = infra(balancers=[balancer("shop", "internet-facing", listener(80, "HTTP"))])
    c = code(SslModeSite(mode="disable", file="db.go", line=3))
    routes = [route("/admin")]
    assert measure_security(m, i, c, routes) == measure_security(m, i, c, routes)


def test_with_no_scans_at_all_every_measure_is_unknown_or_not_applicable():
    states = {m.criterion: m.state for m in measure_security(None, None, None, [])}
    assert states == {
        "EXTERNAL_HTTPS": "UNKNOWN",
        "DB_ACCESS_SCOPE": "UNKNOWN",
        "DB_TRANSPORT_ENCRYPTION": "UNKNOWN",
        "ADMIN_API_EXPOSURE": "UNKNOWN",
    }


# --- the report -------------------------------------------------------------------------------


def run_report(repo, main_go):
    work = repo["work"]
    (work / "main.go").write_text(main_go)
    git(work, "commit", "-q", "--allow-empty", "-am", "x")
    git(work, "push", "-q", str(repo["url"]), "HEAD:refs/heads/main")
    sha = git(work, "rev-parse", "HEAD")
    pipeline = make(repo)
    inp = AnalysisInput(
        migration_id="mig-1", repository=repo["url"], requested_commit=sha, manifests_dir="k8s"
    ).model_dump(mode="json")
    source = pipeline.fetch_source(inp)
    scanned = pipeline.scan_code(inp, source)
    parsed = pipeline.scan_manifests(inp, source)
    infra_result = {"status": "NOT_COLLECTED", "reason": "AWS_SCAN_NOT_CONFIGURED"}
    built = pipeline.build_requirements(inp, source, scanned, parsed, infra_result)
    pre = pipeline.precheck(inp, source, scanned, parsed)
    report = pipeline.check_evidence(
        inp, source, built, infra_result, pre, {"status": "NOT_RUN", "rows_ref": None}
    )
    return pipeline.store.get_json(ArtifactRef.model_validate(report["report_ref"]))


def test_the_report_carries_the_four_measures_with_evidence_from_the_source(repo):
    source = MAIN_GO.replace(
        '_ = os.Getenv("DB_HOST")', 'dsn := "host=db sslmode=disable"\n\t_ = dsn'
    )
    full = run_report(repo, source)
    measures = {m["criterion"]: m for m in full["security"]}
    assert list(measures) == CRITERIA
    encryption = measures["DB_TRANSPORT_ENCRYPTION"]
    assert encryption["state"] == "UNPROTECTED"
    assert encryption["evidence"][0]["kind"] == "CODE"
    assert encryption["evidence"][0]["locator"].startswith("main.go:")
    assert "host=db" not in str(full["security"])  # only the mode word, never the string


def test_the_report_without_any_mode_says_unknown(repo):
    full = run_report(repo, MAIN_GO)
    encryption = next(m for m in full["security"] if m["criterion"] == "DB_TRANSPORT_ENCRYPTION")
    assert encryption["state"] == "UNKNOWN"
