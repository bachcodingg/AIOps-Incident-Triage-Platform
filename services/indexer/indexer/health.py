"""Liveness and readiness for a worker that has no HTTP API of its own.

Liveness fails when the main loop stops heartbeating (stuck, not just idle),
readiness fails until the first full reindex has finished. Metrics are pushed
over OTLP (see telemetry.py), so there is no /metrics here.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthState:
    def __init__(self, stale_after_seconds: float = 30.0, clock=time.monotonic):
        self._clock = clock
        self._stale_after = stale_after_seconds
        self._last_beat = clock()
        self.ready = False

    def beat(self) -> None:
        self._last_beat = self._clock()

    def alive(self) -> bool:
        return self._clock() - self._last_beat < self._stale_after


def serve(state: HealthState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                ok, body = state.alive(), {"status": "ok" if state.alive() else "stale"}
            elif self.path == "/readyz":
                ok, body = state.ready, {"status": "ready" if state.ready else "indexing"}
            else:
                ok, body = False, {"error": "not_found"}
            payload = json.dumps(body).encode()
            self.send_response(200 if ok else (404 if "error" in body else 503))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep probe traffic out of the logs
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    return server
