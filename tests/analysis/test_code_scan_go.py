from pathlib import Path

from cmp_scanners.code import scan_code
from cmp_scanners.go_adapter import GoAdapter

APP = Path(__file__).parent.parent / "fixtures" / "analysis" / "go_app"


def sites(scan, kind):
    return {(s.target, s.file, s.line) for s in scan.call_sites if s.kind == kind}


def test_environment_reads_include_getenv_lookupenv_and_wrapper_helpers():
    scan = scan_code(APP, {}, [GoAdapter()])
    assert sites(scan, "ENV_READ") == {
        ("DATABASE_URL", "main.go", 13),
        ("ENV_NAME", "main.go", 14),
        ("FEATURE_X", "main.go", 15),
    }


def test_commented_out_calls_and_helper_definitions_are_ignored():
    scan = scan_code(APP, {}, [GoAdapter()])
    targets = {s.target for s in scan.call_sites}
    assert "COMMENTED_OUT" not in targets
    assert not any(s.line == 29 and s.file == "main.go" for s in scan.call_sites)


def test_aws_sdk_calls_are_found_as_service_action_with_file_and_line():
    scan = scan_code(APP, {}, [GoAdapter()])
    calls = {
        (s.target, s.line) for s in scan.call_sites if s.kind == "AWS_SDK" and s.detail == "call"
    }
    assert calls == {("s3.PutObject", 20), ("sqs.SendMessage", 21)}
    imports = {s.target for s in scan.call_sites if s.kind == "AWS_SDK" and s.detail == "import"}
    assert imports == {"s3", "sqs"}


def test_external_hosts_come_from_literal_urls_without_query_or_credentials():
    scan = scan_code(APP, {}, [GoAdapter()])
    hosts = {(s.target, s.line) for s in scan.call_sites if s.kind == "EXTERNAL_HOST"}
    assert hosts == {("api.payments.example.com", 22), ("internal-billing.example.net:8443", 23)}
    dumped = scan.model_dump_json()
    assert "abc123" not in dumped and "key=" not in dumped


def test_database_drivers_are_found_from_imports():
    scan = scan_code(APP, {}, [GoAdapter()])
    assert {s.target for s in scan.call_sites if s.kind == "DB_DRIVER"} == {
        "github.com/jackc/pgx/v5",
        "github.com/jackc/pgx/v5/pgxpool",
        "github.com/lib/pq",
    }


def test_go_files_are_scanned_and_env_comparison_is_complete():
    scan = scan_code(APP, {".env": ["DATABASE_URL", "UNUSED_VAR"]}, [GoAdapter()])
    assert scan.unscannable == []
    assert scan.env_comparison.partial is False
    assert scan.env_comparison.only_in_env == ["UNUSED_VAR"]
    assert scan.env_comparison.missing_from_env == ["ENV_NAME", "FEATURE_X"]


def test_credentials_inside_a_literal_url_never_reach_the_result(tmp_path):
    (tmp_path / "a.go").write_text(
        'package a\nfunc f() { http.Get("https://svc-user:p4ssw0rd@vault.example.org:8200/v1") }\n'
    )
    scan = scan_code(tmp_path, {}, [GoAdapter()])
    assert [s.target for s in scan.call_sites] == ["vault.example.org:8200"]
    assert "p4ssw0rd" not in scan.model_dump_json()


def test_external_host_sites_record_the_url_scheme_so_the_port_can_be_inferred():
    scan = scan_code(APP, {}, [GoAdapter()])
    schemes = {s.target: s.detail for s in scan.call_sites if s.kind == "EXTERNAL_HOST"}
    assert schemes == {
        "api.payments.example.com": "https",
        "internal-billing.example.net:8443": "http",
    }
