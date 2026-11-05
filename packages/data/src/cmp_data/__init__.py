"""데이터 이전(CDC) 판단 로직. 외부 I/O 없음(ticket data-migration 01)."""

from .contracts import (
    PrecheckFacts,
    PrecheckResult,
    ReadinessPolicy,
    ReadinessSample,
    ReplicationNames,
    SyncSample,
    TableSync,
)
from .events import TABLE_STATES, readiness_event, sync_events
from .logic import evaluate_precheck, evaluate_readiness, replication_names, sync_phase

__all__ = [
    "TABLE_STATES",
    "PrecheckFacts",
    "PrecheckResult",
    "ReadinessPolicy",
    "ReadinessSample",
    "ReplicationNames",
    "SyncSample",
    "TableSync",
    "evaluate_precheck",
    "evaluate_readiness",
    "readiness_event",
    "replication_names",
    "sync_events",
    "sync_phase",
]
