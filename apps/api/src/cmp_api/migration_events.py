"""Migration event store, operational log and emitters (ticket 24, specs/provisioning/migration-events.md).

`migration_events` is append-only: `seq` grows by one per Migration inside a transaction and a
UNIQUE idempotency key keeps one row per (workflow, activity, attempt, type, key). Data is checked
against the web contract (`cmp_contracts.events`), masked and size-limited before it is stored.
Emitting is observation: a rejected or failed write never fails the caller (E9).
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cmp_contracts.events import PHASES, STATUSES, MigrationEvent, utc_now, validate_data
from cmp_scanners.masking import MASK, mask_text
from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from cmp_api.db import Base

logger = logging.getLogger(__name__)

MAX_DATA_BYTES = 32 * 1024
MAX_FIELDS_BYTES = 8 * 1024
SEQ_RETRIES = 8
LOG_LEVELS = ("debug", "info", "warning", "error")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|credential|api[_-]?key|private[_-]?key|access[_-]?key)"
)
_ID_ID = BigInteger().with_variant(Integer, "sqlite")  # SQLite autoincrement needs INTEGER


class MigrationEventRow(Base):
    __tablename__ = "migration_events"
    __table_args__ = (
        UniqueConstraint("migration_id", "seq", name="uq_migration_events_seq"),
        UniqueConstraint("migration_id", "idempotency_key", name="uq_migration_events_idempotency"),
    )

    id: Mapped[int] = mapped_column(_ID_ID, primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(String(120), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)


class MigrationLogRow(Base):
    __tablename__ = "migration_logs"

    id: Mapped[int] = mapped_column(_ID_ID, primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    at: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    activity: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    fields: Mapped[dict] = mapped_column(JSON, nullable=False)


class EventRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EventStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Actor:
    """Who emitted: an Activity attempt, a Worker process, or the API."""

    source: str = "api"  # activity | worker | api
    workflow_id: str = "-"
    activity_id: str = "-"
    attempt: int = 0
    activity: str | None = None


API_ACTOR = Actor()


# ---- masking and checks (E4, E5, E7) ----


def _is_identifier(key: str) -> bool:
    return key in ("id", "key", "seq") or key.endswith(
        ("_id", "digest", "_uri", "_lsn", "_ref", "_refs")
    )


def mask_value(value: Any, key: str = "") -> Any:
    """Mask secrets in a JSON value. Secret-named string fields are replaced whole (references
    ending in `_ref` stay); identifiers are kept; other strings go through `mask_text`."""
    if isinstance(value, dict):
        return {k: mask_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(item, key) for item in value]
    if not isinstance(value, str):
        return value
    if key and _SECRET_KEY.search(key) and not key.endswith(("_ref", "ref", "_refs")):
        return MASK
    if _is_identifier(key):
        return value
    return mask_text(value)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def idempotency_hash(actor: Actor, event_type: str, key: str) -> str:
    raw = f"{actor.workflow_id}/{actor.activity_id}/{actor.attempt}/{event_type}/{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class PreparedEvent:
    migration_id: str
    type: str
    phase: str
    status: str
    progress: float | None
    data: dict
    idempotency_key: str


def prepare_event(
    migration_id: str,
    event_type: str,
    phase: str,
    status: str,
    data: dict,
    *,
    progress: float | None,
    idempotency_key: str | None,
    actor: Actor,
) -> PreparedEvent:
    if not migration_id or len(migration_id) > 120:
        raise EventRejected("EVENT_INVALID")
    if phase not in PHASES or status not in STATUSES or event_type == "snapshot":
        raise EventRejected("EVENT_INVALID")
    if progress is not None and not (0 <= progress <= 100):
        raise EventRejected("EVENT_INVALID")
    try:
        shaped = validate_data(event_type, dict(data or {}))
    except ValueError:
        raise EventRejected("EVENT_INVALID") from None
    masked = mask_value(shaped)
    if _size(masked) > MAX_DATA_BYTES:
        raise EventRejected("EVENT_TOO_LARGE")
    key = idempotency_key
    if key is None:
        key = hashlib.sha256(
            json.dumps(masked, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return PreparedEvent(
        migration_id,
        event_type,
        phase,
        status,
        None if progress is None else round(float(progress), 2),
        masked,
        idempotency_hash(actor, event_type, key),
    )


def prepare_log(
    migration_id: str, level: str, code: str, fields: dict | None, actor: Actor
) -> dict:
    if level not in LOG_LEVELS or not _CODE.match(code or "") or not migration_id:
        raise EventRejected("LOG_INVALID")
    masked = mask_value(dict(fields or {}))
    if _size(masked) > MAX_FIELDS_BYTES:
        masked = {"truncated": True}
    return {
        "migration_id": migration_id,
        "at": utc_now(),
        "level": level,
        "source": actor.source,
        "activity": actor.activity,
        "attempt": actor.attempt,
        "code": code,
        "fields": masked,
    }


# ---- business DB ----


def _event(row: MigrationEventRow) -> MigrationEvent:
    return MigrationEvent(
        migration_id=row.migration_id,
        seq=row.seq,
        at=row.at,
        phase=row.phase,
        type=row.type,
        status=row.status,
        progress=row.progress,
        data=row.data,
    )


def store_event(session: Session, event: PreparedEvent) -> MigrationEvent:
    """Append with the next `seq` (E6); an existing idempotency key returns the stored event."""
    for attempt in range(SEQ_RETRIES):
        existing = session.scalar(
            select(MigrationEventRow).where(
                MigrationEventRow.migration_id == event.migration_id,
                MigrationEventRow.idempotency_key == event.idempotency_key,
            )
        )
        if existing is not None:
            return _event(existing)
        last = session.scalar(
            select(func.max(MigrationEventRow.seq)).where(
                MigrationEventRow.migration_id == event.migration_id
            )
        )
        row = MigrationEventRow(
            migration_id=event.migration_id,
            seq=(last or 0) + 1,
            at=utc_now(),
            phase=event.phase,
            type=event.type,
            status=event.status,
            progress=event.progress,
            data=event.data,
            idempotency_key=event.idempotency_key,
        )
        session.add(row)
        try:
            session.commit()
        except (IntegrityError, OperationalError):
            # Another writer took this seq (or the key); re-read and try again.
            session.rollback()
            time.sleep(0.005 * (attempt + 1))
            continue
        return _event(row)
    raise EventStoreError("SEQ_ALLOCATION_FAILED")


def append_event(
    session: Session,
    migration_id: str,
    event_type: str,
    phase: str,
    status: str,
    data: dict,
    *,
    progress: float | None = None,
    idempotency_key: str | None = None,
    actor: Actor = API_ACTOR,
) -> MigrationEvent:
    """Validate, mask and store one event. Raises `EventRejected` for invalid input."""
    prepared = prepare_event(
        migration_id,
        event_type,
        phase,
        status,
        data,
        progress=progress,
        idempotency_key=idempotency_key,
        actor=actor,
    )
    return store_event(session, prepared)


def list_events(
    session: Session, migration_id: str, *, after: int = 0, limit: int = 500
) -> list[MigrationEvent]:
    rows = session.scalars(
        select(MigrationEventRow)
        .where(MigrationEventRow.migration_id == migration_id, MigrationEventRow.seq > after)
        .order_by(MigrationEventRow.seq)
        .limit(limit)
    )
    return [_event(row) for row in rows]


def list_logs(session: Session, migration_id: str, *, after: int = 0, limit: int = 500) -> list:
    rows = session.scalars(
        select(MigrationLogRow)
        .where(MigrationLogRow.migration_id == migration_id, MigrationLogRow.id > after)
        .order_by(MigrationLogRow.id)
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "at": row.at,
            "level": row.level,
            "source": row.source,
            "activity": row.activity,
            "attempt": row.attempt,
            "code": row.code,
            "fields": row.fields,
        }
        for row in rows
    ]


# ---- emitters (E8, E9) ----


class EventEmitter(Protocol):
    def emit(
        self,
        migration_id: str,
        event_type: str,
        phase: str,
        status: str,
        data: dict,
        *,
        progress: float | None = None,
        idempotency_key: str | None = None,
        actor: Actor = API_ACTOR,
    ) -> MigrationEvent | None: ...

    def log(
        self,
        migration_id: str,
        level: str,
        code: str,
        fields: dict | None = None,
        *,
        actor: Actor = API_ACTOR,
    ) -> dict | None: ...


class _Emitter:
    """Shared emit/log: check, mask, store; never raise to the caller."""

    def _store_event(self, event: PreparedEvent) -> MigrationEvent:
        raise NotImplementedError

    def _store_log(self, entry: dict) -> dict:
        raise NotImplementedError

    def emit(
        self,
        migration_id,
        event_type,
        phase,
        status,
        data,
        *,
        progress=None,
        idempotency_key=None,
        actor=API_ACTOR,
    ):
        try:
            prepared = prepare_event(
                migration_id,
                event_type,
                phase,
                status,
                data,
                progress=progress,
                idempotency_key=idempotency_key,
                actor=actor,
            )
        except EventRejected as rejected:
            logger.warning("migration event rejected", extra={"code": rejected.code})
            self.log(migration_id, "warning", rejected.code, {"type": event_type}, actor=actor)
            return None
        try:
            return self._store_event(prepared)
        except Exception:  # noqa: BLE001 - observation must not fail the business step
            logger.warning("migration event store failed", extra={"type": event_type})
            return None

    def log(self, migration_id, level, code, fields=None, *, actor=API_ACTOR):
        try:
            return self._store_log(prepare_log(migration_id, level, code, fields, actor))
        except Exception:  # noqa: BLE001 - observation must not fail the business step
            logger.warning("migration log store failed", extra={"code": code})
            return None


class NullEmitter(_Emitter):
    """Default before a Worker configures storage: checks input, stores nothing."""

    def _store_event(self, event: PreparedEvent) -> None:
        return None

    def _store_log(self, entry: dict) -> None:
        return None


class InMemoryEmitter(_Emitter):
    """Test emitter with the same seq/idempotency rules as the database."""

    def __init__(self) -> None:
        self.events: list[MigrationEvent] = []
        self.logs: list[dict] = []
        self._keys: dict[tuple[str, str], MigrationEvent] = {}
        self._lock = threading.Lock()

    def _store_event(self, event: PreparedEvent) -> MigrationEvent:
        with self._lock:
            found = self._keys.get((event.migration_id, event.idempotency_key))
            if found is not None:
                return found
            seq = 1 + sum(1 for e in self.events if e.migration_id == event.migration_id)
            stored = MigrationEvent(
                migration_id=event.migration_id,
                seq=seq,
                at=utc_now(),
                phase=event.phase,
                type=event.type,
                status=event.status,
                progress=event.progress,
                data=event.data,
            )
            self.events.append(stored)
            self._keys[(event.migration_id, event.idempotency_key)] = stored
            return stored

    def _store_log(self, entry: dict) -> dict:
        with self._lock:
            stored = {"id": len(self.logs) + 1, **entry}
            self.logs.append(stored)
            return stored

    def of_type(self, event_type: str) -> list[MigrationEvent]:
        return [e for e in self.events if e.type == event_type]


class DbEventEmitter(_Emitter):
    def __init__(self, session_factory: sessionmaker[Session] | Callable[[], Session]):
        self._sessions = session_factory

    def _store_event(self, event: PreparedEvent) -> MigrationEvent:
        with self._sessions() as session:
            return store_event(session, event)

    def _store_log(self, entry: dict) -> dict:
        with self._sessions() as session:
            row = MigrationLogRow(**entry)
            session.add(row)
            session.commit()
            return {"id": row.id, **entry}
