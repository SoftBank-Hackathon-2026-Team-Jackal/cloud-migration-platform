"""Ticket 31: the Fixer reads a skill when the situation calls for it, not in every run.

Seam: `select_skills` (failure class and reasons -> skill names, from the trigger rules next to the
skills), and `propose_fix` with a scripted model that does or does not call `load_skill`. Nothing
here asks a real model. The skill text is never put in the prompt: the model asks for it.
"""

import json
import re
from pathlib import Path

import yaml
from cmp_agents.fixer import FixInput, propose_fix
from cmp_agents.fixer_skills import load_triggers, select_skills
from cmp_agents.llm import FakeLLMClient, LLMResponse, call

REAL_SKILLS = Path(__file__).parent.parent.parent / "skills"
NETWORK_TEXT = "NETWORK-SKILL-BODY: allow the host, or keep the service at the source."
STORAGE_TEXT = "STORAGE-SKILL-BODY: change the endpoint and the credentials."


def make_skills(tmp_path, rules):
    for name, body in {
        "network": NETWORK_TEXT,
        "storage-s3": STORAGE_TEXT,
        "test-authoring": "T",
    }.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(body)
    (tmp_path / "fixer-triggers.yaml").write_text(yaml.safe_dump({"triggers": rules}))
    return tmp_path


RULES = [
    {"skill": "network", "reason_patterns": ["^EGRESS_BLOCKED:", "^MIGRATE_SOURCE_CALLED:"]},
    {
        "skill": "storage-s3",
        "failure_classes": ["CODE", "BUILD"],
        "reason_patterns": [r"s3[.-].*amazonaws\.com"],
    },
]


# --- which skill fits which situation ---------------------------------------------------------


def test_a_skill_is_chosen_by_the_reasons_of_the_failure(tmp_path):
    root = make_skills(tmp_path, RULES)
    assert select_skills(root, "SPEC", ["EGRESS_BLOCKED:api.pay.example.com:443"]) == ["network"]
    assert select_skills(root, "SPEC", ["HEALTH_FAILED"]) == []


def test_a_rule_can_limit_the_failure_classes(tmp_path):
    root = make_skills(tmp_path, RULES)
    reasons = ["MIGRATE_SOURCE_CALLED:s3.ap-northeast-2.amazonaws.com"]
    assert select_skills(root, "CODE", reasons) == ["network", "storage-s3"]
    assert select_skills(root, "SPEC", reasons) == ["network"]  # storage-s3 is for code only


def test_a_failure_the_fixer_may_not_fix_gets_no_skill(tmp_path):
    root = make_skills(tmp_path, RULES)
    for klass in ("INFRA", "RENDER"):
        assert select_skills(root, klass, ["EGRESS_BLOCKED:x:443"]) == []


def test_skills_come_once_in_rule_order_and_never_the_testers(tmp_path):
    rules = [*RULES, {"skill": "network", "reason_patterns": ["EGRESS"]}]
    rules.append({"skill": "test-authoring", "reason_patterns": ["EGRESS"]})
    root = make_skills(tmp_path, rules)
    assert select_skills(root, "SPEC", ["EGRESS_BLOCKED:x:443"]) == ["network"]


def test_a_rule_for_a_skill_that_does_not_exist_is_ignored(tmp_path):
    root = make_skills(tmp_path, [{"skill": "ghost", "reason_patterns": ["."]}, *RULES])
    assert select_skills(root, "SPEC", ["EGRESS_BLOCKED:x:443"]) == ["network"]


def test_without_a_triggers_file_or_a_skills_folder_nothing_is_chosen(tmp_path):
    assert select_skills(tmp_path, "CODE", ["EGRESS_BLOCKED:x:443"]) == []
    assert select_skills(tmp_path / "missing", "CODE", ["EGRESS_BLOCKED:x:443"]) == []
    assert select_skills(None, "CODE", ["EGRESS_BLOCKED:x:443"]) == []


def test_the_shipped_trigger_rules_only_name_real_skills_with_valid_patterns():
    triggers = load_triggers(REAL_SKILLS)
    assert triggers  # the rules for the existing skills ship with them
    for trigger in triggers:
        assert (REAL_SKILLS / trigger.skill / "SKILL.md").is_file()
        assert trigger.skill != "test-authoring"
        for pattern in trigger.reason_patterns:
            re.compile(pattern)
    assert select_skills(REAL_SKILLS, "SPEC", ["EGRESS_BLOCKED:x.example.com:443"]) == ["network"]
    assert select_skills(REAL_SKILLS, "SPEC", ["HEALTH_FAILED"]) == []


# --- split by target or not ---------------------------------------------------------------------

S3_CALL = ["MIGRATE_SOURCE_CALLED:s3.ap-northeast-2.amazonaws.com"]
SPLIT = [  # one skill for the source service, one more only for a GCP target
    {"skill": "storage-s3", "reason_patterns": [r"amazonaws\.com"]},
    {"skill": "network", "targets": ["GCP"], "reason_patterns": [r"amazonaws\.com"]},
]
ONE = [{"skill": "storage-s3", "reason_patterns": [r"amazonaws\.com"]}]  # targets are in the skill


def test_a_skill_can_be_limited_to_a_target(tmp_path):
    root = make_skills(tmp_path, SPLIT)
    assert select_skills(root, "CODE", S3_CALL, target_provider="GCP") == ["storage-s3", "network"]
    assert select_skills(root, "CODE", S3_CALL, target_provider="ON_PREM") == ["storage-s3"]
    assert select_skills(root, "CODE", S3_CALL, target_provider="AWS") == ["storage-s3"]


def test_a_rule_for_a_target_does_not_match_when_the_target_is_not_known(tmp_path):
    root = make_skills(tmp_path, SPLIT)
    assert select_skills(root, "CODE", S3_CALL) == ["storage-s3"]  # no target given
    assert select_skills(root, "CODE", S3_CALL, target_provider=None) == ["storage-s3"]


def test_one_skill_without_a_target_rule_serves_every_target(tmp_path):
    root = make_skills(tmp_path, ONE)
    for provider in ("AWS", "GCP", "ON_PREM", None):
        assert select_skills(root, "CODE", S3_CALL, target_provider=provider) == ["storage-s3"]


def test_a_rule_may_name_several_targets_and_unknown_targets_are_refused(tmp_path):
    rules = [{"skill": "network", "targets": ["GCP", "ON_PREM"], "reason_patterns": ["."]}]
    root = make_skills(tmp_path, rules)
    assert select_skills(root, "SPEC", ["x"], target_provider="ON_PREM") == ["network"]
    assert select_skills(root, "SPEC", ["x"], target_provider="AWS") == []


def test_a_rule_with_an_unknown_target_is_a_broken_file_and_offers_nothing(tmp_path):
    (tmp_path / "bad").mkdir()
    rules = [{"skill": "network", "targets": ["AZURE"], "reason_patterns": ["."]}]
    root = make_skills(tmp_path / "bad", rules)
    assert select_skills(root, "SPEC", ["x"], target_provider="AZURE") == []


# --- the Fixer reads the skill only when asked ----------------------------------------------


def submit_spec_fix():
    return LLMResponse(
        tool_calls=[
            call(
                "submit_requirements_fix",
                reason="payment API is called",
                add_intents=[
                    {
                        "source_resource_id": "api",
                        "external_host": "api.pay.example.com",
                        "port": 443,
                    }
                ],
            )
        ]
    )


def fix_input(klass="SPEC"):
    return FixInput(
        failure_class=klass,
        reasons=["EGRESS_BLOCKED:api.pay.example.com:443"],
        requirements_json=json.dumps(
            {
                "contract_version": "2",
                "resources": [
                    {
                        "id": "api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
                        "memory_mib": 128, "port": 8080, "health_path": "/healthz", "env_bindings": {},
                    }
                ],
                "network_intent": [],
            }
        ),
        logs="refused",
        tests={},
    )  # fmt: skip


def tool_names(llm):
    return {tool.name for tool in llm.calls[0].tools}


def test_the_model_can_read_a_chosen_skill_and_the_run_records_it(tmp_path):
    root = make_skills(tmp_path, RULES)
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("load_skill", name="network")]), submit_spec_fix()]
    )
    outcome = propose_fix(fix_input(), llm=llm, skills_root=root, skills=["network"])
    assert outcome.status == "PROPOSED" and outcome.record.skills_loaded == ["network"]
    assert "load_skill" in tool_names(llm)
    assert NETWORK_TEXT in llm.calls[1].messages[-1].content  # the skill came back as a tool result


def test_the_skill_text_is_not_in_the_prompt_only_its_name(tmp_path):
    root = make_skills(tmp_path, RULES)
    llm = FakeLLMClient([submit_spec_fix()])
    propose_fix(fix_input(), llm=llm, skills_root=root, skills=["network"])
    sent = llm.calls[0].system + " ".join(m.content for m in llm.calls[0].messages)
    assert "network" in sent and NETWORK_TEXT not in sent


def test_a_skill_that_was_not_chosen_cannot_be_read(tmp_path):
    root = make_skills(tmp_path, RULES)
    llm = FakeLLMClient(
        [LLMResponse(tool_calls=[call("load_skill", name="storage-s3")]), submit_spec_fix()]
    )
    outcome = propose_fix(fix_input(), llm=llm, skills_root=root, skills=["network"])
    assert outcome.record.skills_loaded == []
    assert STORAGE_TEXT not in llm.calls[1].messages[-1].content
    assert "error" in llm.calls[1].messages[-1].content


def test_without_a_chosen_skill_the_tool_is_not_offered():
    for kwargs in ({}, {"skills": []}):
        llm = FakeLLMClient([submit_spec_fix()])
        propose_fix(fix_input(), llm=llm, **kwargs)
        assert "load_skill" not in tool_names(llm)
        assert "Skills you may load" not in llm.calls[0].messages[0].content


def test_a_chosen_name_without_a_skills_folder_offers_no_tool():
    llm = FakeLLMClient([submit_spec_fix()])
    propose_fix(fix_input(), llm=llm, skills=["network"])
    assert "load_skill" not in tool_names(llm)
