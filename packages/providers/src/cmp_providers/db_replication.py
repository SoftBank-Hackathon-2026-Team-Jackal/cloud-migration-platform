"""Ticket 27: target PostgreSQL logical-replication defaults and the replication-capable check.

One SQL probe and one judge are shared by the AWS/GCP deploy Activity (09·11) and the ON_PREM
Agent (12) so `replication_capable` means the same thing on every target: the login role may
replicate *and* the server can publish (`wal_level=logical`, free slot and sender capacity).
"""

from cmp_contracts.chart import DatabaseParameters, PostgresChartValues

# PG16 defaults for slots/senders (10) are kept explicitly; they exceed the source's 4/4.
DEFAULT_PARAMETERS = {"wal_level": "logical", "max_replication_slots": 10, "max_wal_senders": 10}

# `$1` is the host. Runs inside the postgres container, which already has the password env.
DB_PROBE = (
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
    "-v ON_ERROR_STOP=1 -tAc \"select concat_ws('|', current_setting('server_version_num'), "
    "(select (rolreplication or rolsuper)::text from pg_roles where rolname = current_user), "
    "current_setting('wal_level'), current_setting('max_replication_slots'), "
    "current_setting('max_wal_senders'))\""
)


def default_parameters(provider: str, release_name: str) -> DatabaseParameters:
    node = f"{provider.lower().replace('_', '-')}-{release_name}"[:63]
    return DatabaseParameters.model_validate(DEFAULT_PARAMETERS | {"app": {"node_name": node}})


def judge_probe(probe: str) -> tuple[str, bool, str | None]:
    """`version|role|wal_level|slots|senders` → (version, capable, first failing code)."""
    parts = probe.strip().split("|")
    version = parts[0] if parts else ""
    try:
        _, role, wal_level, slots, senders = parts
        slots_n, senders_n = int(slots), int(senders)
    except ValueError:
        return version, False, "REPLICATION_PROBE_UNREADABLE"
    for failed, code in (
        (role != "true", "REPLICATION_ROLE_MISSING"),
        (wal_level != "logical", "WAL_LEVEL_NOT_LOGICAL"),
        (slots_n <= 0, "REPLICATION_SLOTS_UNAVAILABLE"),
        (senders_n <= 0, "WAL_SENDERS_UNAVAILABLE"),
    ):
        if failed:
            return version, False, code
    return version, True, None


def parameters_summary(values: PostgresChartValues) -> str:
    """Preview suffix for the DATABASE change; empty for Values approved before ticket 27."""
    params = values.database.parameters
    if params is None:
        return ""
    return (
        f" · 논리 복제 설정 wal_level={params.wal_level} · 슬롯 {params.max_replication_slots}"
        f" · 송신자 {params.max_wal_senders}"
    )
