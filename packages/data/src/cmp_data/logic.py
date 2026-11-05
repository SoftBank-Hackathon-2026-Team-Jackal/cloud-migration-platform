"""데이터 이전(CDC) 판단 함수. PoC 스크립트의 판단 부분을 옮겼다.

- 사전 점검: `poc/cdc/scripts/precheck.sql`, 시나리오 03(REPLICA IDENTITY)
- 복제 단계: `poc/cdc/scripts/03_wait_sync.sh`
- 전환 가능 시점: `poc/cdc/scripts/readiness.py`
"""

from __future__ import annotations

import re

from .contracts import (
    Direction,
    PrecheckFacts,
    PrecheckResult,
    ReadinessPolicy,
    ReadinessSample,
    ReplicationNames,
    SyncPhase,
    SyncSample,
)

# 정방향 슬롯 1개. 역방향 슬롯은 대상 DB에 만들어지므로 원본에서는 세지 않는다.
SLOTS_NEEDED = 1
MIN_SERVER_VERSION_NUM = 100000  # 논리 복제(publication/subscription)는 PostgreSQL 10부터


def evaluate_precheck(facts: PrecheckFacts) -> PrecheckResult:
    """원본 DB가 논리 복제를 시작할 수 있는지 판정한다. 막는 조건(blockers)이 없을 때만 ok."""
    blockers: list[str] = []
    warnings: list[str] = []
    if facts.server_version_num < MIN_SERVER_VERSION_NUM:
        blockers.append("VERSION_UNSUPPORTED")
    if facts.wal_level != "logical":
        blockers.append("WAL_LEVEL_NOT_LOGICAL")
    if facts.max_replication_slots - facts.slots_in_use < SLOTS_NEEDED:
        blockers.append("NO_FREE_REPLICATION_SLOT")
    if facts.large_objects:
        warnings.append("LARGE_OBJECTS_NOT_REPLICATED")
    if facts.unlogged_tables:
        warnings.append("UNLOGGED_TABLES_NOT_REPLICATED")
    if facts.extensions:
        warnings.append("EXTENSIONS_NEED_TARGET_INSTALL")
    # PK가 없는 테이블은 UPDATE/DELETE를 발행할 수 없어 원본 쓰기가 실패한다(PoC 시나리오 03).
    # 막지 않고 REPLICA IDENTITY FULL을 거는 조치로 돌린다. 대상에서는 행마다 전체 스캔이 된다.
    actions = [("REPLICA_IDENTITY_FULL", name) for name in facts.tables_without_identity]
    return PrecheckResult(ok=not blockers, blockers=blockers, warnings=warnings, actions=actions)


_KEY = re.compile(r"[^a-z0-9]")


def replication_names(migration_id: str, direction: Direction) -> ReplicationNames:
    """migration_id와 방향마다 고정된 이름. 재시도해도 같은 이름이라 존재 여부로 멱등하게 만든다.

    슬롯 이름은 구독 이름과 같게 둔다(PostgreSQL 기본값). 모두 소문자·숫자·밑줄이고 63자 이하다.
    """
    key = _KEY.sub("", migration_id.lower().removeprefix("migration-"))[:20]
    if not key:
        raise ValueError("INVALID_MIGRATION_ID")
    suffix = {"forward": "fwd", "reverse": "rev"}[direction]
    subscription = f"cmp_{key}_{suffix}"
    return ReplicationNames(
        publication=f"{subscription}_pub", subscription=subscription, slot=subscription
    )


def sync_phase(sample: SyncSample, policy: ReadinessPolicy) -> SyncPhase:
    """구독 테이블 상태와 슬롯 지연으로 복제 단계를 정한다.

    schema: 아직 구독한 테이블이 없음 → copy: 초기 복사 중인 테이블이 있음 →
    catchup: 복사 중 쌓인 변경 적용 중(지연을 모르면 여기에 둔다) → streaming: 실시간 동기화
    """
    if not sample.tables:
        return "schema"
    if any(t.state in ("i", "d", "f") for t in sample.tables):
        return "copy"
    lag = sample.lag_bytes
    if any(t.state != "r" for t in sample.tables) or lag is None or lag > policy.lag_ok_bytes:
        return "catchup"
    return "streaming"


def evaluate_readiness(sample: ReadinessSample, policy: ReadinessPolicy) -> tuple[bool, list[str]]:
    """지금 쓰기를 멈추고 전환해도 되는 순간인지. 아니면 이유 코드를 돌려준다."""
    reasons: list[str] = []
    if sample.longest_xact_ms > policy.max_xact_ms:
        reasons.append("LONG_TRANSACTION")
    if sample.lock_waiters:
        reasons.append("LOCK_WAIT")
    if sample.lag_bytes is None:
        reasons.append("REPLICATION_OFF")
    elif sample.lag_bytes > policy.lag_ok_bytes:
        reasons.append("REPLICATION_LAG")
    return not reasons, reasons
