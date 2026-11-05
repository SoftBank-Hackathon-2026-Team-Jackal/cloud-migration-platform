"""Allow-list egress proxy that runs inside the Sandbox namespace (AV-013).

Standard library only: the file is copied into a ConfigMap and run by a pinned `python` image.
Every request is decided against the allow list and logged as one JSON line on stdout:
{"decision": "ALLOW" | "BLOCK", "host": ..., "port": ..., "method": ...}. No headers, bodies or
credentials are logged.
"""

import http.client
import json
import select
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Allowlist:
    def __init__(self, entries: list[dict]):
        self._exact = {(e["host"].lower(), int(e["port"])) for e in entries}
        self._suffix = [
            (e["host"].lower()[1:], int(e["port"])) for e in entries if e["host"].startswith("*.")
        ]

    def permits(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if (host, port) in self._exact:
            return True
        return any(p == port and host.endswith(suffix) for suffix, p in self._suffix)


def make_handler(allow: Allowlist, log=None):
    log = log or (lambda record: print(json.dumps(record, sort_keys=True), flush=True))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _decide(self, host: str, port: int) -> bool:
            ok = allow.permits(host, port)
            log({"decision": "ALLOW" if ok else "BLOCK", "host": host, "port": port,
                 "method": self.command})  # fmt: skip
            return ok

        def _deny(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def do_CONNECT(self):
            host, _, port = self.path.rpartition(":")
            if not host or not port.isdigit() or not self._decide(host, int(port)):
                return self._deny()
            try:
                upstream = socket.create_connection((host, int(port)), timeout=10)
            except OSError:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            sockets = [self.connection, upstream]
            try:
                while True:
                    readable, _, _ = select.select(sockets, [], [], 30)
                    if not readable:
                        break
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
            finally:
                upstream.close()
                self.close_connection = True

        def _forward(self):
            parts = urllib.parse.urlsplit(self.path)
            if not parts.hostname:
                return self._deny()
            port = parts.port or 80
            if not self._decide(parts.hostname, port):
                return self._deny()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            headers = {k: v for k, v in self.headers.items() if k.lower() != "proxy-connection"}
            try:
                conn = http.client.HTTPConnection(parts.hostname, port, timeout=15)
                conn.request(self.command, target, body=body, headers=headers)
                response = conn.getresponse()
                payload = response.read()
            except OSError:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward

    return Handler


def serve(allow: Allowlist, port: int = 3128, address: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((address, port), make_handler(allow))


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/config/allow.json"
    with open(path) as handle:
        entries = json.load(handle)["allow"]
    serve(Allowlist(entries)).serve_forever()
