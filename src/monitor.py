#!/usr/bin/env python3
"""Strata monitor: web dashboard for the strata LLM engine.

Usage:
    monitor.py [config.json] [--log-file FILE] [--file-position start|end]
               [--port N] [--bind ADDR] [--unit NAME] [--db PATH]
"""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading

from collector import Collector, EventPump, State, _journal_pattern
from server import make_server
from store import Store

# config.json lives in the install root (the dir containing src/), so a
# checkout and an installed tree behave the same
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

DEFAULTS = {
    "unit": "strata@.service",
    "bind": "127.0.0.1",
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
    "metrics_poll": True,     # poll the engine's /metrics for slot + GPU state
    "metrics_url": None,      # null = take the URL from the journal ready: line
    "api_key": None,          # only needed if the strata server requires keys
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
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = dict(DEFAULTS)
    path = args.config or os.environ.get("STRATA_MONITOR_CONFIG",
                                         DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in DEFAULTS or v is not None})
    elif args.config:
        print("config not found: %s" % path, file=sys.stderr)
        sys.exit(2)

    overrides = {"log_file": args.log_file, "file_position": args.file_position,
                 "port": args.port, "bind": args.bind, "unit": args.unit,
                 "db": args.db, "preload_hours": args.preload_hours}
    cfg.update({k: v for k, v in overrides.items() if v is not None})
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
    logging.info("strata monitor on http://%s:%s/  (source: %s)",
                 cfg["bind"], cfg["port"],
                 cfg["log_file"] or "journal:" + _journal_pattern(cfg["unit"]))
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        collector.stop()
        store.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
