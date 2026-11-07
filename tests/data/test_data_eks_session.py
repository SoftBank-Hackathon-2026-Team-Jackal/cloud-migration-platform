"""EKS session cache for data migration Activities: one token per ~10 minutes, not per kubectl."""

import json
import os
import stat

import pytest
from cmp_data import DbEndpoint
from cmp_worker.activities import data_migration as dm
from cmp_worker.activities.k8s_workloads_cli import CommandError

ENDPOINT = DbEndpoint.model_validate(
    {"access": "AWS_EKS", "cluster_name": "cmp-demo-source", "region": "ap-northeast-2",
     "namespace": "chat-source", "pod": "db-0", "container": "postgres", "dbname": "chat",
     "peer_host": "db.chat-source", "peer_port": 5432}
)  # fmt: skip
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


class FakeAws:
    aws = "aws"

    def __init__(self, token_outputs=None):
        self.calls: list[list[str]] = []
        self.tokens = iter(token_outputs or [])

    def run(self, args, **_):
        self.calls.append(args)
        if args[2] == "describe-cluster":
            return json.dumps({"endpoint": "https://eks.example", "ca": "Q0E="}).encode()
        out = next(self.tokens, None)
        if out is not None:
            return out
        status = {
            "expirationTimestamp": "2027-01-15T08:15:00Z",
            "token": f"k8s-aws-v1.t{len(self.calls)}",
        }
        return json.dumps({"kind": "ExecCredential", "status": status}).encode()


@pytest.fixture
def clock():
    now = [NOW]
    return now


def make_cache(fake, clock, profiles=None):
    def factory(profile):
        if profiles is not None:
            profiles.append(profile)
        return fake

    return dm.EksSessionCache(runtime_factory=factory, clock=lambda: clock[0])


def kubeconfig_of(session) -> dict:
    with open(session._kubeconfig) as f:
        return json.load(f)


def test_reuses_one_token_across_sessions_and_writes_no_exec_plugin(clock, monkeypatch):
    monkeypatch.setenv("DATA_MIGRATION_AWS_PROFILE", "data")
    fake = FakeAws()
    cache = make_cache(fake, clock)
    with cache.connect(ENDPOINT) as first:
        config = kubeconfig_of(first)
        mode = stat.S_IMODE(os.stat(first._kubeconfig).st_mode)
    with cache.connect(ENDPOINT) as second:
        assert second._kubeconfig == first._kubeconfig
    assert [c[2] for c in fake.calls] == ["describe-cluster", "get-token"]
    assert config["users"] == [{"name": "eks", "user": {"token": "k8s-aws-v1.t2"}}]
    assert config["clusters"][0]["cluster"]["server"] == "https://eks.example"
    assert mode == 0o600


def test_refreshes_the_token_six_minutes_before_it_expires(clock):
    fake = FakeAws()
    cache = make_cache(fake, clock)
    with cache.connect(ENDPOINT):
        pass
    clock[0] = NOW + 8 * 60  # expires at +15 min; still more than 6 min left
    with cache.connect(ENDPOINT):
        pass
    assert len(fake.calls) == 2
    clock[0] = NOW + 9 * 60
    with cache.connect(ENDPOINT) as session:
        assert kubeconfig_of(session)["users"][0]["user"]["token"] == "k8s-aws-v1.t4"
    assert [c[2] for c in fake.calls] == ["describe-cluster", "get-token"] * 2


def test_profiles_get_separate_entries(clock, monkeypatch):
    profiles: list = []
    fake = FakeAws()
    cache = make_cache(fake, clock, profiles)
    for profile in ("a", "b", "a"):
        monkeypatch.setenv("DATA_MIGRATION_AWS_PROFILE", profile)
        with cache.connect(ENDPOINT):
            pass
    assert profiles == ["a", "b"]


def test_unreadable_token_output_is_a_code_only_error_and_not_cached(clock):
    fake = FakeAws([b'{"status": {}}'])
    cache = make_cache(fake, clock)
    with pytest.raises(CommandError, match="^aws$"), cache.connect(ENDPOINT):
        pass
    with cache.connect(ENDPOINT) as session:
        assert kubeconfig_of(session)["users"][0]["user"]["token"].startswith("k8s-aws-v1.")


def test_a_failed_call_drops_the_cached_token(clock):
    fake = FakeAws()
    cache = make_cache(fake, clock)
    with pytest.raises(CommandError), cache.connect(ENDPOINT):
        raise CommandError("kubectl")
    with cache.connect(ENDPOINT):
        pass
    assert [c[2] for c in fake.calls] == ["describe-cluster", "get-token"] * 2


def test_concurrent_activities_share_one_refresh(clock):
    import threading

    fake = FakeAws()
    cache = make_cache(fake, clock)
    paths = []

    def use():
        with cache.connect(ENDPOINT) as session:
            paths.append(session._kubeconfig)

    threads = [threading.Thread(target=use) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(paths)) == 1
    assert len(fake.calls) == 2
