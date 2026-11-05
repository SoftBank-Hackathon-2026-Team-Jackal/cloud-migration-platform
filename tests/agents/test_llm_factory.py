from cmp_agents.cost import GovernedLLMClient
from cmp_worker.config import Settings
from cmp_worker.llm_factory import EFFORT, build_llm_factory


def config(tmp_path, **kw):
    base = {"llm_enabled": True, "llm_ledger_path": str(tmp_path / "ledger.json")}
    return Settings(_env_file=None, **{**base, **kw})


def test_nothing_is_built_by_default_so_nothing_can_be_spent(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert build_llm_factory(Settings(_env_file=None)) is None
    assert build_llm_factory(config(tmp_path, llm_enabled=False)) is None


def test_it_needs_a_ledger_and_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert build_llm_factory(config(tmp_path, llm_ledger_path=None)) is None
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert build_llm_factory(config(tmp_path)) is None


def test_the_client_is_governed_per_migration_with_the_roles_effort_and_the_configured_limits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    factory = build_llm_factory(config(tmp_path, llm_limit_run_usd=1.5, llm_max_output_tokens=4000))
    mapper, fixer = factory("mig-1", "mapper"), factory("mig-2", "fixer")
    assert isinstance(mapper, GovernedLLMClient) and mapper.run_id == "mig-1"
    assert mapper.ledger.limits.per_run_usd == 1.5 and mapper.max_output_tokens == 4000
    assert mapper.inner.effort == EFFORT["mapper"] == "medium"
    assert fixer.inner.effort == "high" and fixer.inner.model == "claude-sonnet-5-5"
    assert mapper.ledger is fixer.ledger  # one shared budget for the whole worker


def test_the_key_is_never_stored_in_settings(tmp_path):
    assert "anthropic" not in {f.lower() for f in Settings.model_fields if "key" in f.lower()}
