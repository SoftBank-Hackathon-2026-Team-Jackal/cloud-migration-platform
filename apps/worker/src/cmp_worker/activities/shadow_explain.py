"""그림자 리허설 불일치 설명(Provisioning 84). 마스킹한 표본으로 분류·원인 가설만 만든다.

관찰 결과를 사람에게 설명하는 제안일 뿐이다: 아무것도 고치거나 막지 않는다. 모델이 꺼져 있거나
(ADR-004: `build_llm_factory`가 None) 실패하면 None을 돌려준다. Workflow는 20초 안에 답이 없으면
`ai=null`로 진행한다. 비용 한도는 다른 Agent와 같은 공유 원장(`GovernedLLMClient`)을 따른다.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from cmp_agents.llm import LLMError, Message
from cmp_data.shadow import valid_ai
from cmp_scanners.masking import MASK, mask_text
from temporalio import activity

SYSTEM = """You review a shadow comparison between an application's origin (source cloud) and a
migrated target that reads a replicated copy of the same database. The same read-only GET request
was sent to both and their JSON responses differ. You only explain; nothing will be changed.

Classify the main cause as one category:
- noise: values that legitimately differ per request (timestamps, request ids, ordering, caches)
- data: the target database is missing or has different rows (replication lag or gaps)
- env: environment or configuration differences (hosts, URLs, feature flags, regions, versions)
- code: the application behaves differently (different build or logic)

Answer with JSON only: {"category": "...", "summary": "<one sentence>",
"hypotheses": ["<up to 3 short cause hypotheses>"]}"""

_SECRET_KEY = re.compile(r"(?i)password|passwd|secret|token|credential|api[_-]?key|private[_-]?key")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _mask(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    return mask_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def masked_samples(samples: list[dict]) -> list[dict]:
    """모델에 보내기 전에 가린다: 키 이름이 비밀 같으면 값을 통째로, 나머지는 `mask_text`."""
    out = []
    for sample in samples:
        diff = []
        for item in sample.get("diff") or []:
            pointer = str(item.get("pointer", ""))
            secret = bool(_SECRET_KEY.search(pointer))
            diff.append({"pointer": pointer,
                         "origin": MASK if secret else _mask(item.get("origin")),
                         "target": MASK if secret else _mask(item.get("target"))})  # fmt: skip
        out.append({"method": sample.get("method"), "path": mask_text(str(sample.get("path", ""))),
                    "status_origin": sample.get("status_origin"),
                    "status_target": sample.get("status_target"), "diff": diff})  # fmt: skip
    return out


def parse(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return valid_ai(json.loads(_FENCE.sub("", text.strip())))
    except ValueError:
        return None


def _ask(factory, payload: dict) -> dict | None:
    client = factory(payload["migration_id"], "shadow")
    question = json.dumps({"counts": payload.get("counts") or {},
                           "samples": masked_samples(payload.get("samples") or [])},
                          ensure_ascii=False)  # fmt: skip
    response = client.complete(
        system=SYSTEM, messages=[Message(role="user", content=question)], tools=[]
    )
    return parse(response.text)


async def explain(payload: dict, factory) -> dict | None:
    if factory is None or not payload.get("samples"):
        return None
    try:
        return await asyncio.to_thread(_ask, factory, payload)
    except LLMError as exc:
        activity.logger.info("shadow explain unavailable: %s", exc.code)
        return None


@activity.defn(name="shadow_explain")
async def shadow_explain(payload: dict) -> dict | None:
    """payload: {migration_id, samples(최대 5), counts}. 결과는 `shadow.result.ai` 또는 None."""
    from cmp_worker.llm_factory import build_llm_factory  # 설정 읽기는 실행 때만

    return await explain(payload, build_llm_factory())
