import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = os.environ.get("NAME", "?")
state = {"fail": False, "delay": 0.0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            code = 200
        elif self.path == "/inject/fail":
            state["fail"] = True
            body = b"fail on"
            code = 200
        elif self.path == "/inject/ok":
            state["fail"] = False
            state["delay"] = 0.0
            body = b"fail off"
            code = 200
        elif self.path.startswith("/inject/delay/"):
            state["delay"] = float(self.path.rsplit("/", 1)[1])
            body = b"delay"
            code = 200
        else:
            time.sleep(state["delay"])
            code = 500 if state["fail"] else 200
            body = f"hello from {NAME}".encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
