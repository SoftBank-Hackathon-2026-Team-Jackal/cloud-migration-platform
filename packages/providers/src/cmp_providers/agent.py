"""Durable local polling transport for sandbox Agent integration.

The spool is trusted local storage, not a production authenticated broker.
RUNNING work is never automatically reissued after loss of its worker.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cmp_providers.artifacts import canonical_json_bytes


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, agent TEXT, payload BLOB, status TEXT, result TEXT)"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, job_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload, status, result FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown job")
        return {
            "payload": json.loads(row[0]),
            "status": row[1],
            "result": json.loads(row[2]) if row[2] else None,
        }

    def submit(self, payload: dict):
        data = canonical_json_bytes(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (payload["job_id"],)).fetchone()
            if row and bytes(row[0]) != data:
                raise ValueError("Job ID already belongs to different work")
            db.execute(
                "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,NULL)",
                (payload["job_id"], payload["agent_id"], data, "QUEUED"),
            )
        return self.get(payload["job_id"])

    def claim(self, agent_id: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE agent=? AND status='QUEUED' ORDER BY rowid LIMIT 1",
                (agent_id,),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", row)
        return self.get(row[0])

    def finish(self, job_id: str, status: str, result: dict):
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("Invalid completion status")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status=?, result=? WHERE id=? AND status IN ('RUNNING','UNKNOWN')",
                (status, json.dumps(result), job_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Job is not running")
