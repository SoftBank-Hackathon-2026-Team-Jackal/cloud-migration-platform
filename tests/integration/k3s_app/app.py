"""Disposable integration app: every healthy response proves an authenticated SQL query."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with psycopg.connect(
                host=os.environ["DB_HOST"],
                port=os.environ["DB_PORT"],
                dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
                connect_timeout=3,
            ) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS agent_probe (id integer PRIMARY KEY)"
                )
                connection.execute("INSERT INTO agent_probe VALUES (1) ON CONFLICT DO NOTHING")
                row = connection.execute("SELECT id FROM agent_probe WHERE id=1").fetchone()
            result = {"database": "ok", "persisted": row == (1,)}
            self.send_response(200)
        except (psycopg.Error, KeyError):
            result = {"database": "unavailable"}
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
