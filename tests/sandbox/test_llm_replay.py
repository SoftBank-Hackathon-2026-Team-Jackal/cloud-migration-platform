"""Replaying a model's earlier answer for the same inputs (ticket 39)."""

from cmp_agents.fixer import CodeFix, FileChange, FixOutcome
from cmp_worker.llm_replay import dump_fix, dump_tests, load_fix, replay_key, replayed
from cmp_worker.redteam_cache import RedTeamCacheError


class Cache:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.puts: list[str] = []

    def get(self, key):
        return self.store.get(key)

    def put(self, key, value):
        self.puts.append(key)
        self.store[key] = value


def _fix() -> FixOutcome:
    change = FileChange(path="main.go", content="package main\n")
    return FixOutcome(
        status="PROPOSED", code_fix=CodeFix(files=[change], reason="why"), reason="why"
    )


def run(cache, key="k", produce=None, counter=None):
    calls = counter if counter is not None else []

    def make():
        calls.append(1)
        return (produce or _fix)()

    return replayed(cache, key, make, dump=dump_fix, load=load_fix), calls


def test_the_second_ask_for_the_same_key_never_calls_the_model():
    cache, calls = Cache(), []
    first, _ = run(cache, counter=calls)
    second, _ = run(cache, counter=calls)
    assert len(calls) == 1
    assert second.code_fix == first.code_fix and second.status == "PROPOSED"


def test_a_different_key_asks_again():
    cache, calls = Cache(), []
    run(cache, key="a", counter=calls)
    run(cache, key="b", counter=calls)
    assert len(calls) == 2


def test_a_refusal_is_not_kept_so_the_next_run_asks_again():
    cache, calls = Cache(), []
    refused = lambda: FixOutcome(status="FAILED", reason="x")
    run(cache, produce=refused, counter=calls)
    run(cache, produce=refused, counter=calls)
    assert len(calls) == 2 and cache.puts == []


def test_no_cache_or_no_key_just_asks():
    calls: list[int] = []
    run(None, counter=calls)
    run(Cache(), key=None, counter=calls)
    assert len(calls) == 2


def test_an_unreachable_store_is_a_miss_and_a_failed_write_is_ignored():
    class Broken(Cache):
        def get(self, key):
            raise RedTeamCacheError("CACHE_STORE_ERROR")

        def put(self, key, value):
            raise RedTeamCacheError("CACHE_STORE_ERROR")

    result, calls = run(Broken())
    assert result.status == "PROPOSED" and len(calls) == 1


def test_a_stored_object_that_no_longer_loads_is_a_miss():
    cache, calls = Cache(), []
    cache.store["k"] = {"code_fix": {"unexpected": True}}
    result, _ = run(cache, counter=calls)
    assert result.status == "PROPOSED" and len(calls) == 1


def test_the_key_depends_on_every_part_and_stays_under_the_permitted_prefix():
    a = replay_key("fixer", "src", "req", "CODE")
    assert a == replay_key("fixer", "src", "req", "CODE")
    assert a != replay_key("fixer", "src", "req", "SPEC")
    assert a != replay_key("fixer", "src2", "req", "CODE")
    assert a != replay_key("tester", "src", "req", "CODE")
    assert a.startswith("redteam/")  # the instance role's IAM covers `redteam/*` only


def test_a_failed_test_generation_is_not_kept():
    class Failed:
        status = "FAILED"

    assert dump_tests(Failed()) is None
