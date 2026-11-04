from pathlib import Path

import pytest
from cmp_scanners.code import CallSite, scan_code


class FakeAdapter:
    """Reads `ENV NAME` and `AWS service.action` lines; stands in for a language adapter."""

    language = "fake"
    extensions = (".fake",)

    def scan_file(self, path: str, text: str) -> list[CallSite]:
        sites = []
        for number, line in enumerate(text.splitlines(), start=1):
            kind, _, target = line.partition(" ")
            if kind == "ENV":
                sites.append(CallSite(kind="ENV_READ", target=target, file=path, line=number))
            if kind == "AWS":
                sites.append(CallSite(kind="AWS_SDK", target=target, file=path, line=number))
        return sites


class BrokenAdapter(FakeAdapter):
    def scan_file(self, path, text):
        raise RuntimeError("parser exploded")


def write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root


def test_adapter_call_sites_are_collected_with_relative_file_and_line(tmp_path):
    write(tmp_path, {"pkg/a.fake": "noise\nENV DB_URL\nAWS s3.PutObject\n"})
    scan = scan_code(tmp_path, {}, [FakeAdapter()])
    found = {(s.kind, s.target, s.file, s.line) for s in scan.call_sites}
    assert found == {
        ("ENV_READ", "DB_URL", "pkg/a.fake", 2),
        ("AWS_SDK", "s3.PutObject", "pkg/a.fake", 3),
    }


def test_source_files_without_adapter_are_reported_not_ignored(tmp_path):
    write(
        tmp_path,
        {
            "main.go": "package main\n",
            "tool.py": "print(1)\n",
            "README.md": "# doc\n",
            "a.json": "{}",
        },
    )
    scan = scan_code(tmp_path, {}, [FakeAdapter()])
    assert {(u.file, u.language, u.reason) for u in scan.unscannable} == {
        ("main.go", "go", "NO_ADAPTER"),
        ("tool.py", "python", "NO_ADAPTER"),
    }


def test_adapter_failure_marks_the_file_unscannable_without_aborting(tmp_path):
    write(tmp_path, {"a.fake": "ENV X\n", "b.fake": "ENV Y\n"})
    scan = scan_code(tmp_path, {}, [BrokenAdapter()])
    assert {(u.file, u.reason) for u in scan.unscannable} == {
        ("a.fake", "ADAPTER_ERROR"),
        ("b.fake", "ADAPTER_ERROR"),
    }
    assert "parser exploded" not in scan.model_dump_json()


def test_create_extension_is_found_in_sql_with_file_and_line(tmp_path):
    write(
        tmp_path,
        {
            "db/001.sql": "-- CREATE EXTENSION commented;\nCREATE TABLE t(id int);\n"
            "create extension if not exists pgcrypto;\n"
            'CREATE EXTENSION "uuid-ossp" WITH SCHEMA public;\n',
        },
    )
    scan = scan_code(tmp_path, {}, [])
    assert scan.creates_extensions is True
    assert [(e.name, e.file, e.line) for e in scan.extension_creations] == [
        ("pgcrypto", "db/001.sql", 3),
        ("uuid-ossp", "db/001.sql", 4),
    ]


def test_extension_creation_inside_source_strings_is_found(tmp_path):
    write(tmp_path, {"store.go": "const s = `CREATE EXTENSION IF NOT EXISTS citext;`\n"})
    scan = scan_code(tmp_path, {}, [])
    assert [e.name for e in scan.extension_creations] == ["citext"]


def test_no_extension_creation_is_an_explicit_false(tmp_path):
    write(tmp_path, {"a.sql": "CREATE TABLE t(id int);\n"})
    scan = scan_code(tmp_path, {}, [])
    assert scan.creates_extensions is False and scan.extension_creations == []


def test_env_comparison_splits_unused_and_missing_names(tmp_path):
    write(tmp_path, {"a.fake": "ENV USED\nENV MISSING\n"})
    env_names = {".env": ["USED", "UNUSED"], "sub/.env.example": ["USED", "OTHER_UNUSED"]}
    scan = scan_code(tmp_path, env_names, [FakeAdapter()])
    assert scan.env_comparison.only_in_env == ["OTHER_UNUSED", "UNUSED"]
    assert scan.env_comparison.missing_from_env == ["MISSING"]
    assert scan.env_comparison.partial is False


def test_env_comparison_is_marked_partial_when_files_were_not_scanned(tmp_path):
    write(tmp_path, {"a.fake": "ENV USED\n", "main.go": "package main\n"})
    scan = scan_code(tmp_path, {".env": ["USED", "GO_ONLY"]}, [FakeAdapter()])
    assert scan.env_comparison.partial is True
    assert scan.env_comparison.only_in_env == ["GO_ONLY"]


def test_env_files_and_dependency_directories_are_not_scanned(tmp_path):
    write(
        tmp_path,
        {
            ".env": "ENV SECRET_NAME\n",
            ".env.local": "ENV OTHER\n",
            ".git/hooks/x.fake": "ENV GIT\n",
            "node_modules/p/i.go": "package p\n",
            "vendor/v.fake": "ENV VENDOR\n",
            "src/real.fake": "ENV REAL\n",
        },
    )
    scan = scan_code(tmp_path, {}, [FakeAdapter()])
    assert {s.target for s in scan.call_sites} == {"REAL"}
    assert scan.unscannable == []


def test_oversized_and_binary_files_are_reported_as_skipped(tmp_path):
    (tmp_path / "huge.fake").write_text("ENV A\n" * 400_000)
    (tmp_path / "blob.fake").write_bytes(b"\xff\xfe\x00binary")
    scan = scan_code(tmp_path, {}, [FakeAdapter()])
    assert {(u.file, u.reason) for u in scan.unscannable} == {
        ("huge.fake", "TOO_LARGE"),
        ("blob.fake", "NOT_TEXT"),
    }


def test_missing_workspace_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_code(tmp_path / "nope", {}, [])
