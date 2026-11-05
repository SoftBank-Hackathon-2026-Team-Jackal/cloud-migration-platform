"""실습용 원본 앱: DATABASE_URL로 쉬지 않고 쓰고, 커밋에 성공한 순번을 기록한다.

1초마다 stdout에 {"ok": 성공 수, "err": 실패 수, "last": 마지막 성공 순번, "max_ms": 최대 지연} 한 줄.
커밋 성공한 행은 body가 'lg-<순번>'이므로, 전환 뒤 새 주 DB에서 모두 있는지 세어 잃은 쓰기를 확인한다.
"""

import contextlib
import json
import os
import time

import psycopg

URL = os.environ["DATABASE_URL"]
POD = os.environ.get("HOSTNAME", "local")
RATE = float(os.environ.get("RATE", "50"))
ok = err = seq = 0
max_ms = 0.0
last_print = time.monotonic()
conn = None
while True:
    started = time.monotonic()
    try:
        if conn is None or conn.closed:
            conn = psycopg.connect(URL, autocommit=True, prepare_threshold=None, connect_timeout=3)
        seq += 1
        conn.execute("insert into messages(room_id, body) values (1, %s)", (f"lg-{POD}-{seq}",))
        ok += 1
        max_ms = max(max_ms, (time.monotonic() - started) * 1000)
    except psycopg.Error:
        err += 1
        with contextlib.suppress(psycopg.Error):
            conn and conn.close()
        conn = None
        time.sleep(0.2)
    if time.monotonic() - last_print >= 1:
        print(
            json.dumps({"ok": ok, "err": err, "last": seq, "max_ms": round(max_ms, 1)}), flush=True
        )
        last_print, max_ms = time.monotonic(), 0.0
    time.sleep(max(0.0, 1 / RATE - (time.monotonic() - started)))
