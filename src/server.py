"""Tiny HTTP server: dashboard static files + JSON API."""

import json
import logging
import math
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from store import STAGE_ID  # noqa: F401  (kept for symmetry with store)

log = logging.getLogger("server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "strata-monitor/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    # ---- helpers ------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) and path != STATIC_DIR:
            return self._send(404, {"error": "not found"})
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        ctype = MIME.get(os.path.splitext(path)[1], "application/octet-stream")
        self._send(200, data, ctype)

    # ---- routes -------------------------------------------------------
    def do_GET(self):  # noqa: N802
        try:
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/api/state":
                return self._api_state()
            if url.path == "/api/history":
                return self._api_history(url)
            if url.path == "/api/health":
                return self._send(200, {"ok": True, "now": time.time()})
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("request failed: %s", self.path)
            try:
                self._send(500, {"error": "internal error"})
            except Exception:
                pass

    def _api_state(self):
        st = self.server.state.snapshot(self.server.cfg.get("stale_after_s", 30))
        st["samples"] = self.server.store.count_samples()
        st["history_retention_days"] = self.server.cfg.get("retention_days", 14)
        self._send(200, st)

    def _api_history(self, url):
        q = parse_qs(url.query)
        try:
            window = int(q.get("window", ["3600"])[0])
        except ValueError:
            window = 3600
        window = max(60, min(window, 30 * 86400))
        now = time.time()
        try:
            now = float(q.get("now", [now])[0])
        except ValueError:
            pass
        bucket = max(1, int(math.ceil(window / 600.0)))
        from_ts = int(now - window)
        data = self.server.store.get_history(from_ts, bucket)
        data["gpus"] = self.server.store.get_gpu_history(from_ts, bucket)["gpus"]
        data.update(bucket_s=bucket, window_s=window, now=now,
                    from_ts=from_ts)
        self._send(200, data)


def make_server(state, store, cfg):
    httpd = ThreadingHTTPServer((cfg.get("bind", "127.0.0.1"),
                                 int(cfg.get("port", 8090)), ), Handler)
    httpd.daemon_threads = True
    httpd.state = state
    httpd.store = store
    httpd.cfg = cfg
    return httpd
