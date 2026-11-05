"""데이터 이전 샘플 → 웹 이벤트 `data` 값(`docs/contracts/events.schema.json`).

Activity는 여기서 받은 (type, data)를 그대로 `emit_event_async(migration_id, type, "data_copy",
"running", data)`로 넘긴다. 값과 코드만 담고 화면 문구는 담지 않는다.
"""

from __future__ import annotations

from typing import Any

from .contracts import ReadinessPolicy, ReadinessSample, SyncSample
from .logic import evaluate_readiness, sync_phase

# srsubstate → 웹 data.table.state
TABLE_STATES = {"i": "waiting", "d": "copying", "f": "copied", "s": "copied", "r": "copied"}


def sync_events(sample: SyncSample, policy: ReadinessPolicy) -> list[tuple[str, dict[str, Any]]]:
    """한 번의 복제 샘플 → data.phase, data.lsn(지연을 알 때만), 테이블마다 data.table."""
    events: list[tuple[str, dict[str, Any]]] = [
        ("data.phase", {"phase": sync_phase(sample, policy)})
    ]
    if sample.lag_bytes is not None:
        events.append(
            (
                "data.lsn",
                {
                    "source_lsn": sample.source_lsn,
                    "target_lsn": sample.target_lsn,
                    "lag_bytes": sample.lag_bytes,
                    "lag_ms": sample.lag_ms,
                },
            )
        )
    for table in sample.tables:
        events.append(
            (
                "data.table",
                {
                    "name": table.name,
                    "rows_copied": table.rows_copied,
                    "rows_total": table.rows_total,
                    "state": TABLE_STATES[table.state],
                },
            )
        )
    return events


def readiness_event(sample: ReadinessSample, policy: ReadinessPolicy) -> dict[str, Any]:
    """전환 가능 시점 신호 → data.readiness. 이유 코드는 운영 로그로 따로 남긴다."""
    ready, _ = evaluate_readiness(sample, policy)
    return {
        "longest_tx_ms": sample.longest_xact_ms,
        "lock_waits": sample.lock_waiters,
        "commits_per_sec": sample.commits_per_sec,
        "ready": ready,
    }
