import pytest
from cmp_scanners.source import SourceFetchError, fetch_source

REPO = "https://github.com/acme/demo-app"


@pytest.mark.parametrize(
    "commit",
    ["--upload-pack=x", "-h", "main branch", "ab\ncd", "a\x00b", "", " ", "main\t"],
)
def test_a_commit_that_could_be_read_as_an_option_is_refused_before_git_runs(
    tmp_path, monkeypatch, commit
):
    called = []
    monkeypatch.setattr("cmp_scanners.source.subprocess.run", lambda *a, **k: called.append(a))
    with pytest.raises(SourceFetchError):
        fetch_source(
            REPO, requested_commit=commit, migration_id="m1", workspace_root=tmp_path / "w"
        )
    assert called == []


@pytest.mark.parametrize("repository", ["-oProxy=x", "https://a b/c", "https://x/y\nz", "--bare"])
def test_a_repository_address_that_is_not_a_plain_url_is_refused_before_git_runs(
    tmp_path, monkeypatch, repository
):
    called = []
    monkeypatch.setattr("cmp_scanners.source.subprocess.run", lambda *a, **k: called.append(a))
    with pytest.raises(SourceFetchError):
        fetch_source(
            repository, requested_commit="a" * 40, migration_id="m1",
            workspace_root=tmp_path / "w", allow_local=True,
        )  # fmt: skip
    assert called == []


@pytest.mark.parametrize("commit", ["a" * 40, "main", "release/1.2", "v1.0.0", "feature_x-2"])
def test_ordinary_commits_branches_and_tags_still_pass_the_check(tmp_path, monkeypatch, commit):
    from cmp_scanners.source import check_source_input

    check_source_input(REPO, commit)  # no exception


def test_the_start_input_refuses_the_same_values_before_anything_is_started():
    from cmp_worker.start_input import build_start_input

    ok = build_start_input(project_id="p", repository=REPO, commit="a" * 40)
    assert ok["analysis"]["requested_commit"] == "a" * 40
    for repository, commit in [(REPO, "--x"), ("-oProxy=x", "a" * 40), (REPO, "a b")]:
        with pytest.raises(ValueError):
            build_start_input(project_id="p", repository=repository, commit=commit)
