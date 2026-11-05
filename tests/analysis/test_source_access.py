import json
import subprocess

import pytest
from cmp_contracts.analysis import AnalysisInput
from cmp_contracts.provisioning import VersionRef
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.source_access import (
    DEV_TOKEN_ENV,
    EnvSourceTokenResolver,
    NoSourceTokenResolver,
    SourceAccessError,
    SourceCredential,
    build_source_resolver,
    dev_ref,
    github_repository,
)
from pipeline_support import make
from temporalio.exceptions import ApplicationError

TOKEN = "ghp_exampleNotARealToken0123456789"
REPO = "https://github.com/acme/demo-app"


# ------------------------------------------------------------------ the resolver


def test_the_dev_resolver_reads_one_fixed_variable_for_one_fixed_reference():
    resolver = EnvSourceTokenResolver({DEV_TOKEN_ENV: TOKEN, "OTHER_SECRET": "must-not-be-read"})
    credential = resolver.resolve(dev_ref(), host="github.com", purpose="READ")
    assert credential.token == TOKEN and credential.host == "github.com"
    for ref in (VersionRef(id="OTHER_SECRET", version="1"), VersionRef(id="user-42", version="1")):
        with pytest.raises(SourceAccessError) as exc:
            resolver.resolve(ref, host="github.com", purpose="READ")
        assert exc.value.code == "SOURCE_TOKEN_UNAVAILABLE"


def test_a_token_is_only_handed_out_for_github_and_only_when_it_looks_like_one():
    with pytest.raises(SourceAccessError):
        EnvSourceTokenResolver({DEV_TOKEN_ENV: TOKEN}).resolve(
            dev_ref(), host="evil.example", purpose="READ"
        )
    for bad in ("", "short", "has space in it", "line\nbreak-token-value", "x" * 600):
        with pytest.raises(SourceAccessError):
            EnvSourceTokenResolver({DEV_TOKEN_ENV: bad}).resolve(
                dev_ref(), host="github.com", purpose="READ"
            )


def test_without_a_token_store_a_private_source_cannot_be_read():
    with pytest.raises(SourceAccessError) as exc:
        NoSourceTokenResolver().resolve(dev_ref(), host="github.com", purpose="READ")
    assert exc.value.code == "SOURCE_ACCESS_NOT_CONFIGURED"


def test_the_same_interface_serves_reading_and_later_writing():
    resolver = EnvSourceTokenResolver({DEV_TOKEN_ENV: TOKEN})
    assert resolver.resolve(dev_ref(), host="github.com", purpose="WRITE").token == TOKEN


def test_a_credential_never_prints_its_token():
    credential = SourceCredential(token=TOKEN)
    for text in (
        repr(credential),
        str(credential),
        f"{credential}",
        json.dumps({"c": repr(credential)}),
    ):
        assert TOKEN not in text and "***" in text


def test_the_resolver_is_chosen_by_name_and_an_unknown_name_is_refused():
    assert isinstance(build_source_resolver("none"), NoSourceTokenResolver)
    assert isinstance(build_source_resolver("env"), EnvSourceTokenResolver)
    with pytest.raises(ValueError):
        build_source_resolver("platform-not-yet")


# ----------------------------------------------------------- where a token may go


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/demo-app",
        "https://github.com/acme/demo-app.git",
        "https://github.com/acme/demo-app/",
    ],
)
def test_a_plain_github_repository_passes(url):
    assert github_repository(url) == ("acme", "demo-app")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/demo-app",
        "https://evil.example/acme/demo-app",
        "https://github.com.evil.example/acme/demo-app",
        "https://user:pw@github.com/acme/demo-app",
        "https://github.com@evil.example/acme/demo-app",
        "https://github.com:8443/acme/demo-app",
        "https://github.com/acme/demo-app?x=1",
        "https://github.com/acme/demo-app#frag",
        "https://github.com/acme",
        "https://github.com/acme/demo-app/extra",
        "https://github.com/acme/../demo-app",
        "https://github.com/-bad/demo-app",
        "https://github.com/acme/..",
        "file:///etc/passwd",
        "/local/path",
    ],
)
def test_any_other_address_is_refused_before_a_token_is_read(url):
    with pytest.raises(SourceAccessError) as exc:
        github_repository(url)
    assert exc.value.code == "SOURCE_REPOSITORY_NOT_ALLOWED"


# ------------------------------------------------------------- the analysis input


def test_the_reference_is_part_of_the_input_and_only_a_reference_fits():
    data = AnalysisInput(
        migration_id="m", repository=REPO, requested_commit="a" * 40, source_access_ref=dev_ref()
    )
    assert data.model_dump(mode="json")["source_access_ref"] == {"id": "dev-env", "version": "1"}
    assert AnalysisInput(migration_id="m").source_access_ref is None
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(
            {"migration_id": "m", "source_access_ref": "ghp_a-token-string"}
        )
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(
            {"migration_id": "m", "source_access_ref": {"id": "x", "version": "1", "token": TOKEN}}
        )


# ------------------------------------------------------- the pipeline and its history


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, repository, **kw):
        self.calls.append((repository, kw))
        from cmp_scanners.source import SourceSnapshot

        return SourceSnapshot(
            workspace="/ws/x", commit="a" * 40, commit_verified=False, reused=False, env_names={}
        )


def pipeline_with(repo, monkeypatch, resolver):
    recorder = Recorder()
    monkeypatch.setattr("cmp_worker.analysis_pipeline.checkout_source", recorder)
    pipeline = make(repo, "ta", source_resolver=resolver)
    return pipeline, recorder


def private_input(**kw):
    return AnalysisInput(
        migration_id="mig-1",
        repository=REPO,
        requested_commit="a" * 40,
        source_access_ref=dev_ref(),
        **kw,
    ).model_dump(mode="json")


def test_the_token_reaches_the_clone_only_when_a_reference_is_given(repo, monkeypatch):
    pipeline, recorder = pipeline_with(
        repo, monkeypatch, EnvSourceTokenResolver({DEV_TOKEN_ENV: TOKEN})
    )
    result = pipeline.fetch_source(private_input())
    assert recorder.calls[0][1]["token"] == TOKEN
    assert TOKEN not in json.dumps(result)  # the step's result carries names and paths only
    public = {**private_input(), "source_access_ref": None}
    pipeline.fetch_source(public)
    assert recorder.calls[1][1]["token"] is None


def test_an_address_that_is_not_github_never_gets_the_token(repo, monkeypatch):
    pipeline, recorder = pipeline_with(
        repo, monkeypatch, EnvSourceTokenResolver({DEV_TOKEN_ENV: TOKEN})
    )
    bad = {**private_input(), "repository": "https://evil.example/acme/demo-app"}
    with pytest.raises(SourceAccessError):
        pipeline.fetch_source(bad)
    assert recorder.calls == []


def test_without_a_resolver_a_reference_fails_instead_of_cloning_without_a_token(repo, monkeypatch):
    pipeline, recorder = pipeline_with(repo, monkeypatch, None)
    with pytest.raises(SourceAccessError) as exc:
        pipeline.fetch_source(private_input())
    assert exc.value.code == "SOURCE_ACCESS_NOT_CONFIGURED" and recorder.calls == []


@pytest.mark.asyncio
async def test_the_activity_turns_a_refusal_into_a_failure_that_is_not_retried(repo, monkeypatch):
    pipeline, _ = pipeline_with(repo, monkeypatch, None)
    with pytest.raises(ApplicationError) as exc:
        await AnalysisActivities(pipeline).fetch_source(private_input())
    assert exc.value.type == "SOURCE_ACCESS_NOT_CONFIGURED" and exc.value.non_retryable
    assert TOKEN not in str(exc.value)


# ------------------------------------------------ the token never rides on the command line


def test_the_real_clone_passes_the_token_in_the_environment_and_never_in_the_arguments(
    tmp_path, monkeypatch
):
    from cmp_scanners.source import fetch_source

    seen = []

    monkeypatch.setattr(
        "cmp_scanners.source.subprocess.run", lambda c, **kw: fake_result(c, kw, seen)
    )
    with pytest.raises(Exception) as exc:
        fetch_source(
            REPO,
            requested_commit="a" * 40,
            migration_id="m1",
            workspace_root=tmp_path / "ws",
            token=TOKEN,
        )
    assert TOKEN not in str(exc.value)
    command, env = seen[0]
    assert TOKEN not in " ".join(command) and "x-access-token" not in " ".join(command)
    assert "GIT_CONFIG_VALUE_0" in env and TOKEN not in json.dumps([c for c, _ in seen])


def fake_result(command, kw, seen):
    seen.append((list(command), dict(kw.get("env") or {})))
    return subprocess.CompletedProcess(
        command, 1, stdout="", stderr=f"fatal: could not read from {REPO} using {TOKEN}"
    )
