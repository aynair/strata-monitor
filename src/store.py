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
           "gpu_vram_total"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER PRIMARY KEY,
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
    gpu_vram_total INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

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
    gpu_vram_total = COALESCE(excluded.gpu_vram_total, samples.gpu_vram_total)
""".format(cols=",".join(COLUMNS), ph=",".join("?" * len(COLUMNS)))


def _needs_migration(con):
    """True if an old samples table exists without the unique ts primary key."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='samples'"
    ).fetchone()
    if row is None:
        return False
    return "PRIMARY KEY (ts)" not in (row[0] or "")


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
            log.info("rebuilding samples table (pre-1.0 schema detected)")
            self._db.execute("DROP TABLE IF EXISTS samples")
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
            self._db.commit()

    # ---- reads --------------------------------------------------------
    def get_history(self, from_ts, bucket_s):
        """Bucketed series: averaged metrics per bucket, stage of the newest row."""
        avg = ", ".join(
            "AVG(%s) AS %s" % (c, c)
            for c in ("tok_s", "cpu_cores", "context", "prompt", "gen",
                      "sys_ram_pct", "sys_load", "gpu_util", "gpu_vram_used",
                      "gpu_vram_total"))
        sql = """
        SELECT g.bts, g.n, g.tok_s, g.cpu_cores, g.context, g.prompt, g.gen,
               g.sys_ram_pct, g.sys_load, g.gpu_util, g.gpu_vram_used,
               g.gpu_vram_total, s.stage
        FROM (
            SELECT (ts / ?) * ? AS bts, COUNT(*) AS n, MAX(ts) AS maxts, {avg}
            FROM samples WHERE ts >= ? GROUP BY bts
        ) g JOIN samples s ON s.ts = g.maxts
        ORDER BY g.bts
        """.format(avg=avg)
        with self._lock:
            rows = self._db.execute(
                sql, (bucket_s, bucket_s, int(from_ts))).fetchall()
        out = {"ts": [], "stage": [], "tok_s": [], "cpu": [], "context": [],
               "prompt": [], "gen": [], "sys_ram_pct": [], "sys_load": [],
               "gpu_util": [], "gpu_vram_used": [], "gpu_vram_total": []}
        keys = ["tok_s", "cpu", "context", "prompt", "gen", "sys_ram_pct",
                "sys_load", "gpu_util", "gpu_vram_used", "gpu_vram_total"]
        for r in rows:
            out["ts"].append(r[0])
            out["stage"].append(STAGES[r[12]] if r[12] < len(STAGES) else "idle")
            for i, k in enumerate(keys):
                out[k].append(r[2 + i])
        return out

    def count_samples(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()
