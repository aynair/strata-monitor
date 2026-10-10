#!/usr/bin/env python3
"""Strata monitor: web dashboard for the strata LLM engine.

Usage:
    monitor.py [config.json] [--log-file FILE] [--file-position start|end]
               [--port N] [--bind ADDR] [--unit NAME] [--db PATH]
               [--tls-cert FILE --tls-key FILE | --no-tls]
"""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading

from collector import Collector, EventPump, State
from server import make_server
from store import Store
from tls import ensure_cert

DEFAULTS = {
    "unit": "strata.service",
    "bind": "0.0.0.0",
    "port": 8090,
    "log_file": None,
    "file_position": "end",
    "preload_hours": 24,
    "sample_interval_s": 1.0,
    "idle_interval_s": 10.0,
    "gpu_interval_s": 3.0,
    "retention_days": 14,
    "stale_after_s": 30,
    "db": "/var/lib/strata-monitor/monitor.db",
    "tls_cert": None,
    "tls_key": None,
}


def load_config(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", nargs="?", default=None, help="path to config.json")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--file-position", choices=["start", "end"], default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None)
    ap.add_argument("--unit", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--preload-hours", type=int, default=None)
    ap.add_argument("--tls-cert", default=None, help="PEM certificate (default: auto self-signed)")
    ap.add_argument("--tls-key", default=None, help="PEM private key")
    ap.add_argument("--no-tls", action="store_true", help="serve plain HTTP (local testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = dict(DEFAULTS)
    path = args.config or os.environ.get("STRATA_MONITOR_CONFIG",
                                         "/etc/strata-monitor/config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in DEFAULTS or v is not None})
    elif args.config:
        print("config not found: %s" % path, file=sys.stderr)
        sys.exit(2)

    overrides = {"log_file": args.log_file, "file_position": args.file_position,
                 "port": args.port, "bind": args.bind, "unit": args.unit,
                 "db": args.db, "preload_hours": args.preload_hours,
                 "tls_cert": args.tls_cert, "tls_key": args.tls_key}
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    cfg["tls"] = not args.no_tls
    return cfg, args.verbose


def main(argv=None):
    cfg, verbose = load_config(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout)

    db = cfg["db"]
    if db != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    store = Store(db)
    saved_ctx = store.get_meta("context_window")
    if saved_ctx and not cfg.get("context_window"):
        cfg["context_window"] = int(saved_ctx)

    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    state = State(cfg["unit"], host=host)
    if saved_ctx:
        state.update(context_window=int(saved_ctx))
    for key in ("model", "server_url"):
        v = store.get_meta(key)
        if v:
            state.update(**{key: v})

    pump = EventPump(state, store)
    collector = Collector(cfg, state, store, pump)
    if cfg["tls"]:
        cfg["tls_cert"], cfg["tls_key"] = ensure_cert(cfg)
    httpd = make_server(state, store, cfg)

    stop = threading.Event()

    def shutdown(signum, _frame):
        logging.info("signal %s: shutting down", signum)
        stop.set()
        collector.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    collector.start()
    scheme = "https" if cfg["tls"] else "http"
    logging.info("strata monitor on %s://%s:%s/  (source: %s)",
                 scheme, cfg["bind"], cfg["port"],
                 cfg["log_file"] or "journal:" + cfg["unit"])
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        collector.stop()
        store.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
