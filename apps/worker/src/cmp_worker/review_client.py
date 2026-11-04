"""Query-then-Signal access to the analysis child's review gate (used by the CLI).

The API will follow the same rule: ask the child for its review state first and only send a
Signal when it is waiting for exactly that decision.
"""

from typing import Any, Literal

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

_SIGNALS = {"confirm": "confirm_requirements", "approve": "approve_losses"}


class ReviewNotPendingError(Exception):
    pass


def child_id(migration_id: str) -> str:
    return f"analysis-{migration_id}"


async def get_review_state(client: Client, migration_id: str) -> dict[str, Any]:
    try:
        return await client.get_workflow_handle(child_id(migration_id)).query("review_state")
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise ReviewNotPendingError("no analysis found for this migration") from exc
        raise


async def send_review_signal(
    client: Client,
    migration_id: str,
    kind: Literal["confirm", "approve"],
    payload: dict[str, Any],
) -> None:
    state = await get_review_state(client, migration_id)
    expected = _SIGNALS[kind]
    if state.get("pending") != expected:
        raise ReviewNotPendingError(
            f"analysis is not waiting for {expected} (state: {state.get('phase')})"
        )
    await client.get_workflow_handle(child_id(migration_id)).signal(expected, payload)


def _pairs(items: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key or "=" in value:
            raise ValueError(f"expected row_id=value, got '{item}'")
        pairs.append((key, value))
    return pairs


def parse_required(items: list[str]) -> dict[str, bool]:
    values = {"true": True, "false": False}
    result = {}
    for key, value in _pairs(items):
        if value.lower() not in values:
            raise ValueError(f"required must be true or false, got '{value}'")
        result[key] = values[value.lower()]
    return result


def parse_approvals(items: list[str]) -> dict[str, str]:
    result = {}
    for key, value in _pairs(items):
        if value.upper() not in {"APPROVED", "REJECTED"}:
            raise ValueError(f"decision must be APPROVED or REJECTED, got '{value}'")
        result[key] = value.upper()
    return result
