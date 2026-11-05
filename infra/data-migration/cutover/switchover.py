#!/usr/bin/env python3
"""DB 전환 실행기(specs/data-migration DM-010, ticket 04). `cmp-cutover` Pod의 runner 컨테이너에서
Activity가 `kubectl exec -i … python3 /opt/cmp/switchover.py`로 실행하고, 설정은 stdin JSON으로 받는다
(비밀번호가 명령줄·환경변수에 남지 않게). 결과는 stdout JSON 한 줄, 진행 기록은 stderr.

PoC `poc/cdc/scripts/05_switchover_fast.py`를 옮긴 것이다. 순서와 결과 키(steps_ms)는 같다.

PAUSE 전
  0a. 새 주 DB에 발행 준비, 옛 주 DB에 역방향 구독을 꺼진 상태로 생성(origin = none)
  0b. 새 주 DB 시퀀스를 옛 주 값 + seq_headroom 으로 미리 올림
  0c. 동기 복제 창: 옛 주에 synchronous_standby_names = <정방향 구독>, 앱은 synchronous_commit = local
  0d. 전환 가능 시점 대기(가장 긴 트랜잭션, 잠금 대기, 지연)
PAUSE 안
  1. PgBouncer PAUSE
  2. marker 커밋(remote_apply): 새 주에 반영되면 돌아온다
  3. 두 DB 스냅샷 고정 + 시퀀스 확인(병렬)
  4. 정방향 구독 DISABLE + 역방향 구독 ENABLE(병렬)
  5. 새 주 생존 확인 → PgBouncer 대상 변경 → RECONNECT → RESUME
RESUME 후
  6. 동기 복제 창 해제, 정방향 구독 삭제
  7. 3에서 고정한 스냅샷으로 두 DB 비교

설정(JSON): old_url, new_url, pgb_admin_url, pgb_db, route_file, new_route, fwd_sub, rev_pub,
rev_sub, rev_conninfo, marker_table, max_xact_ms, lag_ok_bytes, wait_ready_sec, pause_timeout_sec,
marker_timeout_ms, seq_headroom, verify("checksum"|"light"|"auto"|"none"), app_name

종료 코드: 0 전환 성공(검증 일치 또는 생략), 3 전환했지만 검증 불일치, 4 전환하지 않음(원래 DB로 재개)
"""

import contextlib
import json
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from psycopg.pq import ExecStatus


class Abort(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def log(msg: str) -> None:
    t = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(t))
    print(f"[{stamp}.{int(t * 1000) % 1000:03d}] {msg}", file=sys.stderr, flush=True)


def now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def one(conn, query, params=None):
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


class Bouncer:
    """PgBouncer 관리 콘솔. simple query 프로토콜만 받으므로 libpq를 직접 쓴다."""

    def __init__(self, url: str):
        self.url = url
        self.conn = psycopg.connect(url, autocommit=True, connect_timeout=5)

    def cmd(self, query: str, timeout: float | None = None) -> None:
        pg = self.conn.pgconn
        pg.send_query(query.encode())
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            pg.consume_input()
            if not pg.is_busy():
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError(query)
            select.select([pg.socket], [], [], remaining)
        err = None
        while (res := pg.get_result()) is not None:
            if res.status == ExecStatus.FATAL_ERROR:
                err = (res.error_message or b"").decode().strip()
        if err:
            raise Abort("PGBOUNCER_COMMAND_FAILED", f"{query} {err}")

    def reopen(self) -> None:
        with contextlib.suppress(psycopg.Error):
            self.conn.close()
        self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=5)


SEQ_QUERY = """select format('%I.%I', schemaname, sequencename), last_value
               from pg_sequences where last_value is not null"""


def read_sequences(conn) -> dict:
    return dict(conn.execute(SEQ_QUERY).fetchall())


def set_sequences(conn, values: dict) -> None:
    with conn.transaction():
        for name, v in values.items():
            conn.execute("select setval(%s::regclass, %s, true)", (name, v))


def readiness(conn, slot: str, app_name: str) -> dict:
    row = conn.execute(
        """
        with a as (
          select xact_start, wait_event_type from pg_stat_activity
          where backend_type = 'client backend' and pid <> pg_backend_pid()
            and coalesce(application_name, '') <> %(app)s)
        select coalesce((select (extract(epoch from now() - min(xact_start)) * 1000)::bigint
                         from a), 0),
               (select count(*) from a where wait_event_type = 'Lock'),
               (select coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)),
                                -1)::bigint
                from pg_replication_slots where slot_name = %(slot)s)""",
        {"app": app_name, "slot": slot},
    ).fetchone()
    return {"longest_xact_ms": row[0], "lock_waiters": row[1], "lag_bytes": row[2]}


def direct_clients(conn, app_name: str) -> int:
    """옛 주 DB에 PgBouncer를 거치지 않고 붙은 세션 수. PgBouncer와 이 실행기는 같은 Pod라 출발지 주소가
    같으므로, 내 접속 주소(inet_client_addr)와 다른 클라이언트를 센다. 이런 세션이 있으면 전환 뒤에도
    옛 주 DB에 쓸 수 있어(롤링 재시작 중인 옛 앱 Pod 등) 새 주 DB에서 커밋된 쓰기가 사라진다."""
    return one(
        conn,
        """select count(*) from pg_stat_activity
           where backend_type = 'client backend' and pid <> pg_backend_pid()
             and coalesce(application_name, '') <> %s
             and client_addr is distinct from inet_client_addr()""",
        (app_name,),
    )


def is_ready(s: dict, cfg: dict) -> tuple[bool, list[str]]:
    reasons = []
    if s["longest_xact_ms"] > cfg["max_xact_ms"]:
        reasons.append("LONG_TRANSACTION")
    if s["lock_waiters"]:
        reasons.append("LOCK_WAIT")
    if not 0 <= s["lag_bytes"] <= cfg["lag_ok_bytes"]:
        reasons.append("REPLICATION_LAG")
    return not reasons, reasons


def begin_snapshot(conn) -> dict:
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    return read_sequences(conn)


def table_summary(conn, mode: str) -> dict:
    tables = [
        r[0]
        for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' order by 1"
        ).fetchall()
    ]
    out = {}
    for t in tables:
        if mode == "checksum":
            q = sql.SQL(
                "select count(*), coalesce(sum(('x' || substr(md5(t::text), 1, 16))"
                "::bit(64)::bigint::numeric), 0) from public.{} t"
            ).format(sql.Identifier(t))
        else:
            has_id = one(
                conn,
                """select count(*) from pg_index i join pg_attribute a
                   on a.attrelid = i.indrelid and a.attnum = i.indkey[0]
                   where i.indrelid = %s::regclass and i.indisprimary and a.attname = 'id'""",
                (f"public.{t}",),
            )
            if not has_id:
                continue
            q = sql.SQL("select count(*), coalesce(max(id), 0) from public.{}").format(
                sql.Identifier(t)
            )
        out[t] = [str(v) for v in conn.execute(q).fetchone()]
    return out


def write_route(path: str, line: str) -> None:
    with open(path, "w") as f:
        f.write(f"[databases]\n{line}\n")


def switchover(cfg: dict) -> tuple[dict, int]:
    app = cfg.get("app_name", "cmp-data-migration")

    def connect(url):
        return psycopg.connect(
            url, autocommit=True, prepare_threshold=None, connect_timeout=5, application_name=app
        )

    # 접속은 처음에 모두 연다. *_v는 스냅샷 검증용이다.
    old, new = connect(cfg["old_url"]), connect(cfg["new_url"])
    old_v, new_v = connect(cfg["old_url"]), connect(cfg["new_url"])
    pgb = Bouncer(cfg["pgb_admin_url"])
    pool = ThreadPoolExecutor(max_workers=2)
    fwd, rev, rev_pub, marker_table = (
        cfg["fwd_sub"],
        cfg["rev_sub"],
        cfg["rev_pub"],
        cfg["marker_table"],
    )
    pgb_db = cfg["pgb_db"]

    if (
        one(new, "select count(*) from pg_subscription where subname = %s and subenabled", (fwd,))
        != 1
    ):
        raise Abort("FORWARD_SUBSCRIPTION_NOT_ACTIVE")
    lag = one(
        old,
        """select coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)), -1)::bigint
           from pg_replication_slots where slot_name = %s""",
        (fwd,),
    )
    if not 0 <= lag <= cfg["lag_ok_bytes"]:
        raise Abort("REPLICATION_LAG", f"{lag} bytes")

    if direct := direct_clients(old, app):
        raise Abort("DIRECT_CLIENTS", f"{direct} sessions bypass PgBouncer")

    verify_mode = cfg.get("verify", "auto")
    if verify_mode == "auto":
        size = one(old, "select pg_database_size(current_database())")
        verify_mode = "checksum" if size <= 100 << 20 else "light"

    ts: dict[str, int] = {}
    st = {key: False for key in ("prepared", "sync_on", "paused", "fwd_disabled", "rev_enabled",
                                 "retargeted", "switched")}  # fmt: skip
    seq_fixed = 0
    try:
        ts["prepare"] = now_ms()
        log(f"0a. reverse replication prepared disabled ({rev})")
        if not one(new, "select count(*) from pg_publication where pubname = %s", (rev_pub,)):
            new.execute(
                sql.SQL("CREATE PUBLICATION {} FOR ALL TABLES").format(sql.Identifier(rev_pub))
            )
        if one(old, "select count(*) from pg_subscription where subname = %s", (rev,)):
            raise Abort("REVERSE_SUBSCRIPTION_EXISTS")
        st["prepared"] = True
        old.execute(
            sql.SQL(
                "CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION {} "
                "WITH (copy_data = false, origin = none, enabled = false)"
            ).format(sql.Identifier(rev), sql.Literal(cfg["rev_conninfo"]), sql.Identifier(rev_pub))
        )

        ts["seq_prebump"] = now_ms()
        bumped = {k: v + cfg["seq_headroom"] for k, v in read_sequences(old).items()}
        log(f"0b. {len(bumped)} sequences pre-bumped by {cfg['seq_headroom']}")
        set_sequences(new, bumped)

        ts["sync_on"] = now_ms()
        log("0c. sync window on")
        st["sync_on"] = True
        old.execute("ALTER SYSTEM SET synchronous_commit = 'local'")
        old.execute(
            sql.SQL("ALTER SYSTEM SET synchronous_standby_names = {}").format(sql.Literal(fwd))
        )
        old.execute("select pg_reload_conf()")
        deadline = time.monotonic() + 5
        while one(old, "select sync_state from pg_stat_replication where application_name = %s",
                  (fwd,)) != "sync":  # fmt: skip
            if time.monotonic() > deadline:
                raise Abort("SYNC_WINDOW_NOT_READY")
            time.sleep(0.01)
        old.execute("SET synchronous_commit = remote_apply")

        ts["wait_ready"] = now_ms()
        if cfg["wait_ready_sec"] > 0:
            log(f"0d. waiting for a ready moment (max {cfg['wait_ready_sec']}s)")
            while True:
                ok, reasons = is_ready(readiness(old, fwd, app), cfg)
                if ok:
                    break
                if now_ms() - ts["wait_ready"] > cfg["wait_ready_sec"] * 1000:
                    raise Abort("NOT_READY", ",".join(reasons))
                time.sleep(0.02)
        if direct := direct_clients(old, app):
            raise Abort("DIRECT_CLIENTS", f"{direct} sessions bypass PgBouncer")

        ts["start"] = now_ms()
        log("1. PAUSE")
        st["paused"] = True
        try:
            pgb.cmd(f"PAUSE {pgb_db};", timeout=cfg["pause_timeout_sec"])
        except TimeoutError:
            pgb.reopen()
            raise Abort("PAUSE_TIMEOUT") from None
        ts["paused"] = now_ms()

        log("2. marker commit (remote_apply)")
        token = f"{time.time_ns()}"
        timer = threading.Timer(cfg["marker_timeout_ms"] / 1000, old.cancel)
        timer.start()
        try:
            marker = one(
                old,
                sql.SQL("INSERT INTO {} (token) VALUES (%s) RETURNING id").format(
                    sql.SQL(marker_table)
                ),
                (token,),
            )
        except psycopg.errors.QueryCanceled:
            marker = one(
                old, sql.SQL("select id from {} where token = %s").format(sql.SQL(marker_table)),
                (token,),
            )  # fmt: skip
        finally:
            timer.cancel()
        while not one(new, sql.SQL("select count(*) from {} where id = %s").format(
                sql.SQL(marker_table)), (marker,)):  # fmt: skip
            if now_ms() - ts["paused"] > cfg["marker_timeout_ms"]:
                raise Abort("MARKER_TIMEOUT")
            time.sleep(0.002)
        old.execute("SET synchronous_commit = local")
        ts["marker"] = now_ms()

        log("3. snapshot + sequence check")
        f_old, f_new = pool.submit(begin_snapshot, old_v), pool.submit(begin_snapshot, new_v)
        old_seq, new_seq = f_old.result(), f_new.result()
        behind = {k: v for k, v in old_seq.items() if new_seq.get(k, -1) < v}
        if behind:
            set_sequences(new, behind)
            seq_fixed = len(behind)
        ts["snapshot"] = now_ms()

        log("4. forward DISABLE, reverse ENABLE")
        f1 = pool.submit(
            new.execute, sql.SQL("ALTER SUBSCRIPTION {} DISABLE").format(sql.Identifier(fwd))
        )
        f2 = pool.submit(
            old.execute, sql.SQL("ALTER SUBSCRIPTION {} ENABLE").format(sql.Identifier(rev))
        )
        try:
            f1.result()
        finally:
            st["fwd_disabled"] = True
        try:
            f2.result()
        finally:
            st["rev_enabled"] = True
        ts["subs"] = now_ms()

        log("5. liveness → retarget → RESUME")
        try:
            one(new, "select 1")
        except psycopg.OperationalError:
            raise Abort("NEW_PRIMARY_UNREACHABLE") from None
        st["retargeted"] = True
        write_route(cfg["route_file"], cfg["new_route"])
        pgb.cmd("RELOAD;")
        pgb.cmd(f"RECONNECT {pgb_db};")
        pgb.cmd(f"RESUME {pgb_db};")
        st["switched"] = True
        ts["resumed"] = now_ms()
        pause_ms = ts["resumed"] - ts["start"]
        log(f"   write pause {pause_ms}ms")
    except BaseException:
        undo(st, cfg, old, new, pgb)
        for c in (old_v, new_v):
            with contextlib.suppress(psycopg.Error):
                c.execute("ROLLBACK")
        raise

    old.execute("ALTER SYSTEM RESET synchronous_standby_names")
    old.execute("ALTER SYSTEM RESET synchronous_commit")
    old.execute("select pg_reload_conf()")
    log("6. drop forward subscription")
    for _ in range(50):
        try:
            new.execute(sql.SQL("DROP SUBSCRIPTION {}").format(sql.Identifier(fwd)))
            break
        except psycopg.errors.ObjectInUse:
            time.sleep(0.2)
        except psycopg.OperationalError:
            break
    ts["dropped"] = now_ms()

    verify_ok = None
    mismatched: list[str] = []
    if verify_mode != "none":
        log(f"7. verify snapshot ({verify_mode})")
        f_old, f_new = (
            pool.submit(table_summary, old_v, verify_mode),
            pool.submit(table_summary, new_v, verify_mode),
        )
        a, b = f_old.result(), f_new.result()
        verify_ok = a == b
        mismatched = sorted(t for t in set(a) | set(b) if a.get(t) != b.get(t))
    for c in (old_v, new_v):
        c.execute("ROLLBACK")
    ts["verified"] = now_ms()

    result = {
        "impl": "fast",
        "write_pause_ms": pause_ms,
        "verify_after": verify_mode,
        "verify_ok": verify_ok,
        "mismatched_tables": mismatched,
        "seq_fixed_in_pause": seq_fixed,
        "steps_ms": {
            "prepare_reverse (PAUSE 전)": ts["seq_prebump"] - ts["prepare"],
            "seq_prebump (PAUSE 전)": ts["sync_on"] - ts["seq_prebump"],
            "sync_window_on (PAUSE 전)": ts["wait_ready"] - ts["sync_on"],
            "wait_ready (PAUSE 전)": ts["start"] - ts["wait_ready"],
            "pause_wait_inflight": ts["paused"] - ts["start"],
            "marker_wait": ts["marker"] - ts["paused"],
            "snapshot_seq_check": ts["snapshot"] - ts["marker"],
            "switch_subscriptions": ts["subs"] - ts["snapshot"],
            "retarget_resume": ts["resumed"] - ts["subs"],
            "drop_forward (RESUME 후)": ts["dropped"] - ts["resumed"],
            "verify (RESUME 후)": ts["verified"] - ts["dropped"],
        },
    }
    return result, (0 if verify_ok is not False else 3)


def undo(st: dict, cfg: dict, old, new, pgb: Bouncer) -> None:
    """PAUSE 안에서 실패했을 때: 구독 상태를 되돌리고 원래 DB로 재개한다."""
    log("ERROR: switchover failed, restoring the original primary")
    steps = [
        (st["rev_enabled"], lambda: old.execute(sql.SQL("ALTER SUBSCRIPTION {} DISABLE").format(sql.Identifier(cfg["rev_sub"])))),
        (st["fwd_disabled"], lambda: new.execute(sql.SQL("ALTER SUBSCRIPTION {} ENABLE").format(sql.Identifier(cfg["fwd_sub"])))),
        (st["sync_on"], lambda: (old.execute("ALTER SYSTEM RESET synchronous_standby_names"),
                                 old.execute("ALTER SYSTEM RESET synchronous_commit"),
                                 old.execute("select pg_reload_conf()"))),
        (st["paused"], lambda: pgb.cmd(f"RESUME {cfg['pgb_db']};")),
        (st["prepared"], lambda: drop_reverse(old, cfg["rev_sub"])),
    ]  # fmt: skip
    for needed, fn in steps:
        if not needed:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - keep restoring the rest
            log(f"   restore step failed: {type(exc).__name__}")


def drop_reverse(old, name: str) -> None:
    """꺼진 역방향 구독을 지운다. 새 주 DB의 슬롯도 지우되, 닿지 않으면 구독만 지운다."""
    ident = sql.Identifier(name)
    try:
        old.execute(sql.SQL("DROP SUBSCRIPTION IF EXISTS {}").format(ident))
    except psycopg.OperationalError:
        old.execute(sql.SQL("ALTER SUBSCRIPTION {} SET (slot_name = NONE)").format(ident))
        old.execute(sql.SQL("DROP SUBSCRIPTION IF EXISTS {}").format(ident))


def main() -> int:
    cfg = json.load(sys.stdin)
    try:
        result, code = switchover(cfg)
    except Abort as exc:
        print(json.dumps({"switched": False, "code": exc.code, "detail": exc.detail}))
        return 4
    except Exception as exc:  # noqa: BLE001 - 결과 JSON은 항상 낸다. 메시지 첫 줄만(비밀값 없음)
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        print(json.dumps({"switched": False, "code": "RUNNER_ERROR", "detail": detail[:300]}))
        return 4
    print(json.dumps({"switched": True, **result}))
    return code


if __name__ == "__main__":
    sys.exit(main())
