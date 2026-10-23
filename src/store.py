"""SQLite time-series store for strata monitor samples."""

import logging
import sqlite3
import threading
import time

log = logging.getLogger("store")

# stage enum (integer in DB)
STAGES = ["idle", "starting", "prefill", "thinking", "answering",
          "tool_call", "stopped", "error"]
STAGE_ID = {name: i for i, name in enumerate(STAGES)}

COLUMNS = ["ts", "stage", "tok_s", "gen", "prompt", "context", "cpu_cores",
           "rss", "sys_ram_pct", "sys_load", "gpu_util", "gpu_vram_used",
           "gpu_vram_total", "req_running", "req_waiting", "slots_busy",
           "slots_total"]

_SAMPLE_COLS = """    ts INTEGER PRIMARY KEY,
    stage INTEGER NOT NULL,
    tok_s REAL,
    gen INTEGER,
    prompt INTEGER,
    context INTEGER,
    cpu_cores REAL,
    rss INTEGER,
    sys_ram_pct REAL,
    sys_load REAL,
    gpu_util REAL,
    gpu_vram_used INTEGER,
    gpu_vram_total INTEGER,
    req_running INTEGER,
    req_waiting INTEGER,
    slots_busy INTEGER,
    slots_total INTEGER"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
%s
);
CREATE TABLE IF NOT EXISTS gpu_samples (
    ts INTEGER NOT NULL,
    gpu_index INTEGER NOT NULL,
    util REAL,
    vram_used INTEGER,
    vram_total INTEGER,
    temp REAL,
    PRIMARY KEY (ts, gpu_index)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
""" % _SAMPLE_COLS

_UPSERT = """
INSERT INTO samples ({cols}) VALUES ({ph})
ON CONFLICT(ts) DO UPDATE SET
    stage = excluded.stage,
    tok_s = excluded.tok_s,
    gen = excluded.gen,
    prompt = excluded.prompt,
    context = excluded.context,
    cpu_cores = COALESCE(excluded.cpu_cores, samples.cpu_cores),
    rss = COALESCE(excluded.rss, samples.rss),
    sys_ram_pct = COALESCE(excluded.sys_ram_pct, samples.sys_ram_pct),
    sys_load = COALESCE(excluded.sys_load, samples.sys_load),
    gpu_util = COALESCE(excluded.gpu_util, samples.gpu_util),
    gpu_vram_used = COALESCE(excluded.gpu_vram_used, samples.gpu_vram_used),
    gpu_vram_total = COALESCE(excluded.gpu_vram_total, samples.gpu_vram_total),
    req_running = COALESCE(excluded.req_running, samples.req_running),
    req_waiting = COALESCE(excluded.req_waiting, samples.req_waiting),
    slots_busy = COALESCE(excluded.slots_busy, samples.slots_busy),
    slots_total = COALESCE(excluded.slots_total, samples.slots_total)
""".format(cols=",".join(COLUMNS), ph=",".join("?" * len(COLUMNS)))

_GPU_UPSERT = """
INSERT INTO gpu_samples (ts, gpu_index, util, vram_used, vram_total, temp)
VALUES (?,?,?,?,?,?)
ON CONFLICT(ts, gpu_index) DO UPDATE SET
    util = COALESCE(excluded.util, gpu_samples.util),
    vram_used = COALESCE(excluded.vram_used, gpu_samples.vram_used),
    vram_total = COALESCE(excluded.vram_total, gpu_samples.vram_total),
    temp = COALESCE(excluded.temp, gpu_samples.temp)
"""


def _table_cols(con, name):
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % name)]


def _needs_migration(con):
    """True if the samples table exists but predates the ts-primary schema or
    lacks any of the current columns (old installs get them backfilled NULL)."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='samples'"
    ).fetchone()
    if row is None:
        return False
    if "ts INTEGER PRIMARY KEY" not in (row[0] or ""):
        return True
    return not set(COLUMNS).issubset(_table_cols(con, "samples"))


def _migrate(con):
    """Rebuild samples to the current schema, keeping every common column."""
    log.info("migrating samples table to the current schema (preserving rows)")
    have = _table_cols(con, "samples")
    sel = ", ".join(have)
    con.executescript(
        "CREATE TABLE samples_new (%s);" % _SAMPLE_COLS +
        "INSERT OR IGNORE INTO samples_new (%s) SELECT %s FROM samples"
        " ORDER BY ts;" % (sel, sel) +
        "DROP TABLE samples; ALTER TABLE samples_new RENAME TO samples;")


def _row_tuple(s):
    out = {}
    for c in COLUMNS:
        v = s.get(c)
        if c == "stage":
            v = STAGE_ID.get(v, 0)
        out[c] = v
    return tuple(out[c] for c in COLUMNS)


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        if _needs_migration(self._db):
            _migrate(self._db)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ---- writes -------------------------------------------------------
    def insert_sample(self, s):
        self.insert_samples([s])

    def insert_samples(self, rows):
        if not rows:
            return
        values = [_row_tuple(r) for r in rows]
        with self._lock:
            self._db.executemany(_UPSERT, values)
            self._db.commit()

    def insert_gpu_samples(self, ts, gpus):
        """One row per GPU for the given sample timestamp."""
        if not gpus:
            return
        values = [(ts, g.get("index"), g.get("util"), g.get("vram_used"),
                   g.get("vram_total"), g.get("temp")) for g in gpus]
        with self._lock:
            self._db.executemany(_GPU_UPSERT, values)
            self._db.commit()

    def set_meta(self, k, v):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
            self._db.commit()

    def get_meta(self, k, default=None):
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def prune(self, retention_days):
        cutoff = int(time.time() - retention_days * 86400)
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM gpu_samples WHERE ts < ?", (cutoff,))
            self._db.commit()

    # ---- reads --------------------------------------------------------
    def get_history(self, from_ts, bucket_s):
        """Bucketed series: gauges averaged, cumulative counters and stage taken
        from the newest row of each bucket; tok/s averaged over active readings
        only (idle 0.0 rows and prefill NULLs are excluded from the mean), and
        buckets with no active reading (idle/prefill) report 0."""
        avg = ", ".join(
            "AVG(%s) AS %s" % (c, c)
            for c in ("cpu_cores", "sys_ram_pct", "sys_load", "gpu_util",
                      "gpu_vram_used", "gpu_vram_total", "req_running",
                      "req_waiting"))
        sql = """
        SELECT g.bts, g.n, g.tok_s, g.cpu_cores, s.context, s.prompt, s.gen,
               g.sys_ram_pct, g.sys_load, g.gpu_util, g.gpu_vram_used,
               g.gpu_vram_total, s.stage, g.req_running, g.req_waiting
        FROM (
            SELECT (ts / ?) * ? AS bts, COUNT(*) AS n, MAX(ts) AS maxts,
                   COALESCE(AVG(NULLIF(tok_s, 0)), 0.0) AS tok_s, {avg}
            FROM samples WHERE ts >= ? GROUP BY bts
        ) g JOIN samples s ON s.ts = g.maxts
        ORDER BY g.bts
        """.format(avg=avg)
        with self._lock:
            rows = self._db.execute(
                sql, (bucket_s, bucket_s, int(from_ts))).fetchall()
        out = {"ts": [], "stage": [], "tok_s": [], "cpu": [], "context": [],
               "prompt": [], "gen": [], "sys_ram_pct": [], "sys_load": [],
               "gpu_util": [], "gpu_vram_used": [], "gpu_vram_total": [],
               "req_running": [], "req_waiting": []}
        keys = ["tok_s", "cpu", "context", "prompt", "gen", "sys_ram_pct",
                "sys_load", "gpu_util", "gpu_vram_used", "gpu_vram_total",
                "stage_idx", "req_running", "req_waiting"]
        for r in rows:
            out["ts"].append(r[0])
            for i, k in enumerate(keys):
                if k == "stage_idx":
                    out["stage"].append(
                        STAGES[r[12]] if r[12] < len(STAGES) else "idle")
                else:
                    out[k].append(r[2 + i])
        return out

    def get_gpu_history(self, from_ts, bucket_s):
        """Bucketed per-GPU series: [{index, ts, util, vram_used, vram_total,
        temp}, ...]; averaged within a bucket (vram_total taken as the max)."""
        sql = """
        SELECT gpu_index, (ts / ?) * ? AS bts, AVG(util), AVG(vram_used),
               MAX(vram_total), AVG(temp)
        FROM gpu_samples WHERE ts >= ?
        GROUP BY gpu_index, bts
        ORDER BY gpu_index, bts
        """
        with self._lock:
            rows = self._db.execute(
                sql, (bucket_s, bucket_s, int(from_ts))).fetchall()
        by_idx = {}
        for r in rows:
            g = by_idx.setdefault(r[0], {"index": r[0], "ts": [], "util": [],
                                         "vram_used": [], "vram_total": [],
                                         "temp": []})
            g["ts"].append(r[1])
            g["util"].append(r[2])
            g["vram_used"].append(r[3])
            g["vram_total"].append(r[4])
            g["temp"].append(r[5])
        return {"gpus": list(by_idx.values())}

    def count_samples(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()
