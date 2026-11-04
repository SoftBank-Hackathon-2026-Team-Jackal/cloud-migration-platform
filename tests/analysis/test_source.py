import subprocess
from pathlib import Path

import pytest
from cmp_scanners.envfile import parse_env_names
from cmp_scanners.masking import mask_text, mask_tree
from cmp_scanners.source import SourceFetchError, fetch_source

FAKE_SECRET = "s3cr3t-Value-ABCD1234efgh5678IJKL"
FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
FAKE_GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_TOKEN = "tok-SuperSecretTokenValue-98765"


def git(cwd, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """Local bare repo with two commits; the first has no .env, the second adds one."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "app.py").write_text("print('v1')\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "v1")
    first = git(work, "rev-parse", "HEAD")
    (work / "app.py").write_text(
        f'API_KEY = "{FAKE_AWS_KEY}"\nGH = "{FAKE_GH_TOKEN}"\nprint("v2")\n'
    )
    (work / ".env").write_text(f"DB_PASSWORD={FAKE_SECRET}\n# note\nAPP_TOKEN={FAKE_TOKEN}\n")
    git(work, "add", "-f", ".")
    git(work, "commit", "-q", "-m", "v2")
    second = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return {"url": str(bare), "first": first, "second": second, "path": bare}


@pytest.fixture
def root(tmp_path):
    return tmp_path / "workspaces"


def fetch(remote, root, **kw):
    kw.setdefault("migration_id", "migration-1")
    kw.setdefault("requested_commit", remote["second"])
    return fetch_source(remote["url"], workspace_root=root, allow_local=True, **kw)


def test_requested_commit_is_used_and_marked_unverified_without_production_commit(remote, root):
    snap = fetch(remote, root)
    assert snap.commit == remote["second"]
    assert snap.commit_verified is False
    assert (Path(snap.workspace) / "app.py").read_text().endswith('print("v2")\n')


def test_production_commit_wins_and_is_verified(remote, root):
    snap = fetch(remote, root, resolve_production_commit=lambda: remote["first"])
    assert snap.commit == remote["first"]
    assert snap.commit_verified is True
    assert (Path(snap.workspace) / "app.py").read_text() == "print('v1')\n"


def test_unknown_production_commit_falls_back_to_requested(remote, root):
    snap = fetch(remote, root, resolve_production_commit=lambda: "0" * 40)
    assert snap.commit == remote["second"]
    assert snap.commit_verified is False


def test_same_commit_is_not_cloned_again(remote, root):
    first = fetch(remote, root)
    # Remove the remote: a second clone would fail, reuse must not touch it.
    subprocess.run(["rm", "-rf", str(remote["path"])], check=True)
    second = fetch(remote, root)
    assert second.workspace == first.workspace
    assert second.reused is True and first.reused is False


def test_workspace_is_separated_by_migration_and_commit_and_private(remote, root):
    a = fetch(remote, root, migration_id="migration-a")
    b = fetch(remote, root, migration_id="migration-b")
    c = fetch(remote, root, migration_id="migration-a", requested_commit=remote["first"])
    assert len({a.workspace, b.workspace, c.workspace}) == 3
    assert Path(a.workspace).parent.name == "migration-a"
    assert Path(a.workspace).name == remote["second"]
    assert (Path(a.workspace).stat().st_mode & 0o077) == 0


def test_workspace_root_must_be_outside_process_directory(remote, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SourceFetchError):
        fetch(remote, tmp_path / "inside" / "ws")


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", ".."])
def test_migration_id_cannot_escape_the_root(remote, root, bad):
    with pytest.raises(SourceFetchError):
        fetch(remote, root, migration_id=bad)


def test_local_paths_are_rejected_unless_explicitly_allowed(remote, root):
    with pytest.raises(SourceFetchError):
        fetch_source(
            remote["url"],
            requested_commit=remote["second"],
            migration_id="m",
            workspace_root=root,
        )


def test_failure_message_and_git_config_never_contain_the_token(remote, root, tmp_path):
    with pytest.raises(SourceFetchError) as exc:
        fetch_source(
            "https://127.0.0.1:9/none.git",
            requested_commit="a" * 40,
            migration_id="m",
            workspace_root=root,
            token=FAKE_TOKEN,
        )
    assert FAKE_TOKEN not in str(exc.value)
    for path in root.rglob("*"):
        if path.is_file():
            assert FAKE_TOKEN not in path.read_text(errors="ignore")


def test_env_names_are_returned_without_values(remote, root):
    snap = fetch(remote, root)
    assert snap.env_names == {".env": ["APP_TOKEN", "DB_PASSWORD"]}
    dumped = snap.model_dump_json()
    assert FAKE_SECRET not in dumped and FAKE_TOKEN not in dumped


def test_parse_env_names_ignores_values_comments_and_blank_lines():
    text = f"# c\n\nexport A=1\nB = {FAKE_SECRET}\nnot a pair\n1BAD=x\nC=\n"
    assert parse_env_names(text) == ["A", "B", "C"]


def test_masked_tree_has_no_secrets_and_original_is_untouched(remote, root, tmp_path):
    snap = fetch(remote, root)
    masked = tmp_path / "masked"
    mask_tree(Path(snap.workspace), masked)
    body = "\n".join(p.read_text() for p in masked.rglob("*") if p.is_file())
    for secret in (FAKE_SECRET, FAKE_AWS_KEY, FAKE_GH_TOKEN, FAKE_TOKEN):
        assert secret not in body
    assert "DB_PASSWORD" in (masked / ".env").read_text()
    assert not (masked / ".git").exists()
    assert FAKE_SECRET in (Path(snap.workspace) / ".env").read_text()


@pytest.mark.parametrize(
    "text",
    [
        f"key={FAKE_AWS_KEY}",
        f"token: {FAKE_GH_TOKEN}",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
        "postgres://admin:hunter2pass@db.internal/app",
        'password = "correct-horse-battery"',
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        "blob=Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A5ODc2",
    ],
)
def test_mask_text_hides_known_secret_shapes(text):
    masked = mask_text(text)
    assert "[MASKED]" in masked
    for needle in (
        FAKE_AWS_KEY,
        FAKE_GH_TOKEN,
        "MIIabc",
        "hunter2pass",
        "correct-horse-battery",
        "abcdefghijklmnop",
        "Zm9vYmFyQmF6",
    ):
        assert needle not in masked


@pytest.mark.parametrize(
    "text",
    [
        "def get_database_connection_string(): pass",
        "password = os.getenv('DB_PASSWORD')",
        "commit 0123456789abcdef0123456789abcdef01234567",
        "SELECT id, name FROM customers WHERE active = true",
    ],
)
def test_mask_text_keeps_ordinary_code(text):
    assert mask_text(text) == text
