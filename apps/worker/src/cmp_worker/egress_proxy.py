"""Allow-list egress proxy that runs inside the Sandbox namespace (AV-013).

Standard library only: the file is copied into a ConfigMap and run by a pinned `python` image.
Every request is decided against the allow list and logged as one JSON line on stdout:
{"decision": "ALLOW" | "BLOCK", "host": ..., "port": ..., "method": ...}. No headers, bodies or
credentials are logged.
"""

import http.client
import ipaddress
import json
import select
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _lookup(host: str) -> list[str]:
    try:
        return [item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
    except OSError:
        return []


class Allowlist:
    """Allowed (host, port) names, plus optional address ranges (`cidrs`) for CONNECT.

    A range entry allows a name when it resolves into the range on that port (the platform's own
    telemetry ingest, the same address production allows). The proxy then dials the matched address
    itself, so a second lookup cannot send the connection somewhere else.
    """

    def __init__(self, entries: list[dict], cidrs: list[dict] | None = None, resolve=None):
        self._exact = {(e["host"].lower(), int(e["port"])) for e in entries}
        self._suffix = [
            (e["host"].lower()[1:], int(e["port"])) for e in entries if e["host"].startswith("*.")
        ]
        self._nets = [
            (ipaddress.ip_network(c["cidr"], strict=False), int(c["port"])) for c in cidrs or []
        ]
        self._resolve = resolve or _lookup

    def permits(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if (host, port) in self._exact:
            return True
        return any(p == port and host.endswith(suffix) for suffix, p in self._suffix)

    def address_for(self, host: str, port: int) -> str | None:
        """An address of `host` inside an allowed range on `port`, or None."""
        if not self._nets or not any(p == port for _, p in self._nets):
            return None
        host = host.strip("[]").rstrip(".")
        try:
            candidates = [str(ipaddress.ip_address(host))]  # a literal needs no lookup
        except ValueError:
            candidates = self._resolve(host)
        for text in candidates:
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                continue
            if any(p == port and address in net for net, p in self._nets):
                return text
        return None


def make_handler(allow: Allowlist, log=None):
    log = log or (lambda record: print(json.dumps(record, sort_keys=True), flush=True))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _record(self, host: str, port: int, ok: bool) -> bool:
            log({"decision": "ALLOW" if ok else "BLOCK", "host": host, "port": port,
                 "method": self.command})  # fmt: skip
            return ok

        def _decide(self, host: str, port: int) -> bool:
            return self._record(host, port, allow.permits(host, port))

        def _deny(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def do_CONNECT(self):
            host, _, port = self.path.rpartition(":")
            if not host or not port.isdigit():
                return self._deny()
            by_name = allow.permits(host, int(port))
            address = None if by_name else allow.address_for(host, int(port))
            if not self._record(host, int(port), by_name or address is not None):
                return self._deny()
            try:
                upstream = socket.create_connection((address or host, int(port)), timeout=10)
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
        config = json.load(handle)
    serve(Allowlist(config["allow"], config.get("cidrs"))).serve_forever()
