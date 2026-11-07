"""Provisioning 84: 그림자 리허설 불일치 설명 Activity. 모델은 가짜다."""

from __future__ import annotations

import json

from cmp_agents.llm import FakeLLMClient, LLMError, LLMResponse
from cmp_worker.activities import shadow_explain as se
from temporalio.testing import ActivityEnvironment

PAYLOAD = {
    "migration_id": "migration-x",
    "counts": {"compared": 100, "match": 90, "mismatch": 10, "target_5xx": 0},
    "samples": [{"method": "GET", "path": "/api/u?token=sk-abcdefghijklmnopqrstuvwxyz0123456789",
                 "status_origin": 200, "status_target": 200,
                 "diff": [{"pointer": "/password", "origin": "hunter2-very-secret-value",
                           "target": "x"}]}],
}  # fmt: skip
ANSWER = {"category": "data", "summary": "row missing", "hypotheses": ["replication lag"]}


def factory_with(client):
    return lambda migration_id, role: client


async def test_explains_with_masked_samples() -> None:
    client = FakeLLMClient([LLMResponse(text=json.dumps(ANSWER))])
    result = await ActivityEnvironment().run(se.explain, PAYLOAD, factory_with(client))
    assert result == ANSWER
    asked = client.calls[0].messages[0].content
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in asked
    assert "hunter2-very-secret-value" not in asked
    assert "/api/u" in asked and client.calls[0].tools == []


async def test_no_model_or_bad_answer_is_none() -> None:
    assert await ActivityEnvironment().run(se.explain, PAYLOAD, None) is None
    bad = FakeLLMClient([LLMResponse(text="not json")])
    assert await ActivityEnvironment().run(se.explain, PAYLOAD, factory_with(bad)) is None


async def test_model_error_is_none() -> None:
    class Broken:
        def complete(self, **kwargs):
            raise LLMError("LLM_UNAVAILABLE")

    assert await ActivityEnvironment().run(se.explain, PAYLOAD, factory_with(Broken())) is None


def test_fenced_json_is_read() -> None:
    assert se.parse("```json\n" + json.dumps(ANSWER) + "\n```") == ANSWER
