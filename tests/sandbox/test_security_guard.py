"""Ticket 29-B: a Fixer fix may not lower the security the analysis measured.

The Fixer makes a failing app pass; one way to do that is to turn a protection off (`sslmode=disable`
so the app connects to a database that offers no SSL). The fix is refused before anything is
written, and the run goes to a person with the reason. Seam: the pure checks, then `sbx_fix`.
"""

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.analysis import SecurityMeasure
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_scanners.security_judge import judge_security, target_security
from cmp_worker.security_guard import (
    code_fix_lowers_security,
    requirements_fix_lowers_security,
)

from .test_publish import REF
from .test_publish_activities import fix_payload, world

CODE = "SECURITY_LOWERED_BY_FIX:DB_TRANSPORT_ENCRYPTION"


def measure(state, criterion="DB_TRANSPORT_ENCRYPTION"):
    return SecurityMeasure(criterion=criterion, state=state)


class Report:
    """What the checks read from the analysis report."""

    def __init__(self, measures=(), judgements=None, provider="ON_PREM"):
        self.security = list(measures)
        self.target_provider = provider
        self.security_judgements = judgements


def dsn(mode):
    return f'package main\nconst dsn = "host=db sslmode={mode}"\n'


# --- code fixes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("baseline", ["PROTECTED", "PARTIAL", "UNKNOWN"])
def test_a_fix_that_brings_in_a_plaintext_mode_lowers_security(baseline):
    report = Report([measure(baseline)])
    changes = [("db.go", dsn("require"), dsn("disable"))]
    assert code_fix_lowers_security(report, changes) == [CODE]
    assert code_fix_lowers_security(report, [("db.go", "", dsn("prefer"))]) == [CODE]


def test_a_source_that_was_plaintext_to_begin_with_is_not_made_lower_by_one_more_plaintext_line():
    report = Report([measure("UNPROTECTED")])
    assert code_fix_lowers_security(report, [("db.go", "", dsn("disable"))]) == []


def test_keeping_a_plaintext_mode_the_file_already_had_is_not_new():
    report = Report([measure("PARTIAL")])
    old = dsn("disable")
    assert code_fix_lowers_security(report, [("db.go", old, old + "// fixed port\n")]) == []


def test_an_encrypted_mode_or_no_mode_does_not_lower_security():
    report = Report([measure("PROTECTED")])
    assert code_fix_lowers_security(report, [("db.go", dsn("disable"), dsn("verify-full"))]) == []
    assert code_fix_lowers_security(report, [("main.go", "", "package main\n")]) == []


def test_without_a_measure_or_a_report_there_is_nothing_to_protect():
    assert code_fix_lowers_security(None, [("db.go", "", dsn("disable"))]) == []
    assert code_fix_lowers_security(Report([]), [("db.go", "", dsn("disable"))]) == []


# --- requirements fixes -----------------------------------------------------------------------


def requirements(*, with_db=True):
    resources = [
        {
            "id": "api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
            "memory_mib": 128, "port": 8080, "health_path": "/healthz", "env_bindings": {},
        }
    ]  # fmt: skip
    if with_db:
        resources.append(
            {
                "id": "db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                "database": {"name": "app", "username": "app"}, "cpu_millicores": 250,
                "memory_mib": 512, "storage_gib": 1,
            }
        )  # fmt: skip
    return RequirementsV2.model_validate(
        {"contract_version": "2", "resources": resources, "network_intent": []}
    )


def judged_report(**kw):
    measures = [measure("UNPROTECTED", "DB_ACCESS_SCOPE")]
    before = judge_security(measures, target_security("ON_PREM", requirements(), measures))
    return Report(measures, before, **kw)


def test_a_requirements_fix_that_leaves_the_judgements_alone_is_fine():
    assert requirements_fix_lowers_security(judged_report(), requirements()) == []


def test_a_requirements_fix_that_makes_a_judgement_worse_is_refused():
    fixed = requirements(with_db=False)  # the database protection can no longer be shown
    assert requirements_fix_lowers_security(judged_report(), fixed) == [
        "SECURITY_LOWERED_BY_FIX:DB_ACCESS_SCOPE"
    ]


def test_without_a_report_or_judgements_a_requirements_fix_is_not_checked():
    assert requirements_fix_lowers_security(None, requirements()) == []
    assert requirements_fix_lowers_security(Report([], None), requirements()) == []


# --- sbx_fix -----------------------------------------------------------------------------------


def code_fix_llm(content):
    return FakeLLMClient(
        [
            LLMResponse(
                tool_calls=[
                    call(
                        "submit_code_fix",
                        reason="connect",
                        files=[{"path": "db.go", "content": content}],
                    )
                ]
            )
        ]
    )


CODE_FAILURE = {"failure_class": "CODE", "reasons": ["HEALTH_FAILED"]}


@pytest.mark.asyncio
async def test_sbx_fix_refuses_a_code_fix_that_turns_ssl_off_and_writes_nothing(tmp_path):
    llm = code_fix_llm(dsn("disable"))
    activities, store, payload, _, _ = world(
        tmp_path, llm=llm, security=[measure("PROTECTED")], judgements=[]
    )
    result = await activities.sbx_fix(fix_payload(payload, store, classification=CODE_FAILURE))
    assert result == {"status": "FAILED", "reason": CODE}
    assert not list(activities.workspace_root.glob("fix-*"))  # no copy of the source was made


@pytest.mark.asyncio
async def test_sbx_fix_accepts_a_code_fix_that_keeps_the_protection(tmp_path):
    llm = code_fix_llm(dsn("verify-full"))
    activities, store, payload, _, _ = world(
        tmp_path, llm=llm, security=[measure("PROTECTED")], judgements=[]
    )
    result = await activities.sbx_fix(fix_payload(payload, store, classification=CODE_FAILURE))
    assert result["status"] == "PROPOSED" and result["kind"] == "CODE"


@pytest.mark.asyncio
async def test_a_run_without_security_measures_is_not_held_back(tmp_path):
    llm = code_fix_llm(dsn("disable"))
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    result = await activities.sbx_fix(fix_payload(payload, store, classification=CODE_FAILURE))
    assert result["status"] == "PROPOSED"
    assert REF  # the shared fixtures are in use
