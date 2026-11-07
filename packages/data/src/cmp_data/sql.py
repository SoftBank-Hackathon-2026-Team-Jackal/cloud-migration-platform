"""데이터 이전 SQL 문과 결과 해석(ticket 02·03). 실행은 Activity가 DB Pod 안의 psql로 한다.

조회 SQL은 결과를 JSON 한 줄로 돌려준다(`psql -XAt`). 식별자는 `replication_names`가 만든
소문자·숫자·밑줄 이름만 넣고, 비밀번호 같은 값은 `literal`/`conninfo`로 감싼다.
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import PrecheckFacts, ReadinessSample, ReplicationNames, SyncSample, TableSync

REPL_ROLE = "cmp_repl"  # 구독자가 발행 DB에 접속할 때 쓰는 전용 역할(REPLICATION + SELECT)
TOOL_APP_NAME = "cmp-data-migration"  # 이 도구의 접속. readiness 신호에서 뺀다
MARKER_TABLE = "public.cmp_switchover_marker"  # 전환 때 마지막 변경 도착 확인용(PoC marker)
# 이 도구가 DB에 만드는 내부 표(schema.table). 사용자 표 비교·스키마 복사·전환 검증 결과에서 뺀다.
INTERNAL_TABLES = frozenset({MARKER_TABLE})
# 스키마 덤프에서 뺄 내부 객체. 표만 빼면 그 bigserial 시퀀스가 주인 없는 시퀀스로 덤프에 남는다. 그러면 대상에 남아
# 표를 지워도 사라지지 않고, 같은 대상으로 다시 이전할 때 복원이 "already exists"로 멈췄다(2026-11-07).
INTERNAL_DUMP_EXCLUDES = INTERNAL_TABLES | frozenset({f"{MARKER_TABLE}_id_seq"})


def literal(value: str) -> str:
    """SQL 문자열 상수."""
    return "'" + value.replace("'", "''") + "'"


def conninfo(**parts: str | int) -> str:
    """libpq 연결 문자열. 값의 `\\`와 `'`를 이스케이프하고 작은따옴표로 감싼다."""
    out = []
    for key, value in parts.items():
        text = str(value).replace("\\", "\\\\").replace("'", "\\'")
        out.append(f"{key}='{text}'")
    return " ".join(out)


# ---- 사전 점검 (poc/cdc/scripts/precheck.sql) ----

PRECHECK_SQL = """
select json_build_object(
  'server_version_num', current_setting('server_version_num')::int,
  'wal_level', current_setting('wal_level'),
  'max_replication_slots', current_setting('max_replication_slots')::int,
  'slots_in_use', (select count(*) from pg_replication_slots),
  'tables_without_identity', coalesce((
    select json_agg(format('%I.%I', n.nspname, c.relname) order by 1)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
      and n.nspname not like 'pg_toast%' and c.relpersistence = 'p'
      and c.relreplident in ('d', 'n')
      and not exists (select 1 from pg_index i where i.indrelid = c.oid and i.indisprimary)), '[]'),
  'sequences', coalesce((select json_agg(format('%I.%I', schemaname, sequencename) order by 1)
                         from pg_sequences), '[]'),
  'large_objects', (select count(*) from pg_largeobject_metadata),
  'unlogged_tables', (select count(*) from pg_class where relkind = 'r' and relpersistence = 'u'),
  'extensions', coalesce((select json_agg(extname order by extname) from pg_extension
                          where extname <> 'plpgsql'), '[]'))
"""


def parse_precheck(raw: str) -> PrecheckFacts:
    return PrecheckFacts.model_validate(json.loads(raw))


def replica_identity_full_sql(tables: list[str]) -> str:
    """`schema.table`(precheck가 format('%I.%I')로 만든 값)에 REPLICA IDENTITY FULL."""
    return "".join(f"alter table {t} replica identity full;\n" for t in tables)


# ---- 준비 ----

WAL_LEVEL_SQL = "select current_setting('wal_level')"
# 대상이 역방향 복제(롤백)의 발행자가 되려면 logical이어야 한다. 재시작해야 적용된다.
SET_WAL_LOGICAL_SQL = "alter system set wal_level = 'logical'"

ENSURE_MARKER_SQL = f"""
create table if not exists {MARKER_TABLE} (
  id bigserial primary key, token text not null, at timestamptz not null default now());
"""

USER_TABLES_SQL = """
select coalesce(json_agg(format('%I.%I', schemaname, relname) order by 1), '[]')
from pg_stat_user_tables
"""


def parse_user_tables(raw: str) -> set[str]:
    """`USER_TABLES_SQL` 결과에서 내부 표를 뺀 사용자 표."""
    return set(json.loads(raw)) - INTERNAL_TABLES


def ensure_repl_role_sql(password: str) -> str:
    """전용 복제 역할을 만들거나 비밀번호를 맞춘다. 재실행해도 결과가 같다."""
    pw = literal(password)
    return f"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname = '{REPL_ROLE}') then
    execute format('create role {REPL_ROLE} login replication password %L', {pw});
  else
    execute format('alter role {REPL_ROLE} login replication password %L', {pw});
  end if;
end $$;
grant usage on schema public to {REPL_ROLE};
grant select on all tables in schema public to {REPL_ROLE};
alter default privileges in schema public grant select on tables to {REPL_ROLE};
"""


def publication_exists_sql(names: ReplicationNames) -> str:
    return f"select count(*) from pg_publication where pubname = '{names.publication}'"


def create_publication_sql(names: ReplicationNames) -> str:
    return f"create publication {names.publication} for all tables"


def subscription_exists_sql(names: ReplicationNames) -> str:
    return f"select count(*) from pg_subscription where subname = '{names.subscription}'"


def create_subscription_sql(
    names: ReplicationNames, *, host: str, port: int, dbname: str, password: str, copy_data: bool
) -> str:
    """대상(구독자)에서 실행한다. 원본 슬롯을 만들고 copy_data면 그 시점 스냅샷으로 초기 복사한다."""
    info = conninfo(host=host, port=port, dbname=dbname, user=REPL_ROLE, password=password)
    flag = "true" if copy_data else "false"
    return (
        f"create subscription {names.subscription} connection {literal(info)} "
        f"publication {names.publication} with (copy_data = {flag}, slot_name = '{names.slot}')"
    )


def drop_subscription_sql(names: ReplicationNames) -> str:
    return f"drop subscription if exists {names.subscription}"


def drop_publication_sql(names: ReplicationNames) -> str:
    return f"drop publication if exists {names.publication}"


# ---- 동기화 감시 (poc/cdc/scripts/03_wait_sync.sh) ----


def target_sync_sql(names: ReplicationNames) -> str:
    """대상: 구독 테이블 상태, 복사한 행 수(복사 중이면 COPY 진행 수), 수신 LSN."""
    return f"""
select json_build_object(
  'tables', coalesce((
    select json_agg(json_build_object(
      'name', format('%I.%I', n.nspname, c.relname),
      'state', r.srsubstate,
      'rows_copied', greatest(coalesce(p.tuples_processed, 0), coalesce(st.n_live_tup, 0)))
      order by c.relname)
    from pg_subscription_rel r
    join pg_subscription s on s.oid = r.srsubid
    join pg_class c on c.oid = r.srrelid
    join pg_namespace n on n.oid = c.relnamespace
    left join pg_stat_user_tables st on st.relid = c.oid
    left join pg_stat_progress_copy p on p.relid = c.oid
    where s.subname = '{names.subscription}'), '[]'),
  'target_lsn', (select max(latest_end_lsn)::text from pg_stat_subscription
                 where subname = '{names.subscription}'))
"""


def source_sync_sql(names: ReplicationNames) -> str:
    """원본: 현재 LSN, 슬롯 지연(슬롯이 없으면 null), 테이블별 전체 행 수(근사치)."""
    return f"""
select json_build_object(
  'source_lsn', pg_current_wal_lsn()::text,
  'lag_bytes', (select pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint
                from pg_replication_slots where slot_name = '{names.slot}'),
  'rows_total', coalesce((select json_object_agg(format('%I.%I', schemaname, relname),
                                                 n_live_tup)
                          from pg_stat_user_tables), '{{}}'))
"""


def parse_sync(target_raw: str, source_raw: str) -> SyncSample:
    target, source = json.loads(target_raw), json.loads(source_raw)
    totals: dict[str, Any] = source.get("rows_total") or {}
    tables = [
        TableSync(
            name=t["name"],
            state=t["state"],
            rows_copied=max(int(t["rows_copied"] or 0), 0),
            rows_total=max(int(totals.get(t["name"]) or 0), 0),
        )
        for t in target["tables"]
    ]
    lag = source.get("lag_bytes")
    return SyncSample(
        tables=tables,
        lag_bytes=None if lag is None else max(int(lag), 0),
        source_lsn=source.get("source_lsn") or "",
        target_lsn=target.get("target_lsn"),
    )


# ---- 전환 가능 시점 (poc/cdc/scripts/readiness.py) ----


def readiness_sql(names: ReplicationNames) -> str:
    """현재 쓰기 주체에서 실행한다. 커밋 수는 두 번 읽어 차이로 초당 커밋을 계산한다."""
    return f"""
with a as (
  select xact_start, wait_event_type from pg_stat_activity
  where backend_type = 'client backend' and pid <> pg_backend_pid()
    and coalesce(application_name, '') <> '{TOOL_APP_NAME}')
select json_build_object(
  'longest_xact_ms', coalesce((select (extract(epoch from now() - min(xact_start)) * 1000)::bigint
                               from a), 0),
  'lock_waiters', (select count(*) from a where wait_event_type = 'Lock'),
  'lag_bytes', (select pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint
                from pg_replication_slots where slot_name = '{names.slot}'),
  'xact_commit', (select xact_commit from pg_stat_database where datname = current_database()))
"""


def parse_readiness(first_raw: str, second_raw: str, seconds: float) -> ReadinessSample:
    first, second = json.loads(first_raw), json.loads(second_raw)
    commits = max(int(second["xact_commit"]) - int(first["xact_commit"]), 0)
    lag = second.get("lag_bytes")
    return ReadinessSample(
        longest_xact_ms=max(int(second["longest_xact_ms"]), 0),
        lock_waiters=int(second["lock_waiters"]),
        lag_bytes=None if lag is None else max(int(lag), 0),
        commits_per_sec=round(commits / seconds, 1) if seconds > 0 else 0,
    )
