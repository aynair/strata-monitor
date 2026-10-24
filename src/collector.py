"""Collects strata events from the journal (or a log file) and system metrics."""

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import psutil

from parser import parse_line
from store import STAGE_ID

log = logging.getLogger("collector")

ACTIVE_STAGES = {"prefill", "thinking", "answering", "tool_call"}

# /metrics is authoritative for the live stage/speed while its data is this fresh
METRICS_FRESH_S = 3.0


def _journal_pattern(unit):
    """journalctl -u accepts globs: the template 'strata@.service' becomes
    'strata@*.service' so every model instance is followed."""
    if "@." in unit:
        base, _, suffix = unit.partition("@.")
        return "%s@*.%s" % (base, suffix)
    return unit


def _num_or_none(s, cast):
    try:
        return cast(float(s))
    except ValueError:
        return None


def parse_nvidia_smi(out, tail=4):
    """All rows of nvidia-smi's CSV output -> per-GPU dicts. The trailing
    `tail` fields are positional, so a name containing a comma cannot shift
    them. tail=6 appends the negotiated PCIe link width and generation."""
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < tail + 2:
            continue
        try:
            gpu = {"index": int(parts[0]),
                   "name": " ".join(",".join(parts[1:-tail]).split()),
                   "util": _num_or_none(parts[-tail], float),
                   "vram_used": _num_or_none(parts[-tail + 1], int),
                   "vram_total": _num_or_none(parts[-tail + 2], int),
                   "temp": _num_or_none(parts[-tail + 3], float)}
            if tail >= 6:
                gpu["link_width"] = _num_or_none(parts[-2], int)
                gpu["link_gen"] = _num_or_none(parts[-1], int)
        except ValueError:
            continue
        gpus.append(gpu)
    return gpus


def apply_gpus(state, gpus):
    """Publish per-GPU readings plus the aggregate scalars the single-GPU API
    always had: mean load, summed VRAM, hottest card, names joined. PCIe link
    info is nvidia-smi-only and near-static, so re-attach it from the cache
    onto readings that arrive without it (the engine's /metrics path)."""
    links = state.get("gpu_links") or {}
    for g in gpus:
        if "link_width" not in g:
            li = links.get(g.get("index"))
            if li:
                g["link_width"], g["link_gen"] = li
    def vals(key):
        return [g[key] for g in gpus if g.get(key) is not None]
    util, used = vals("util"), vals("vram_used")
    total, temps = vals("vram_total"), vals("temp")
    names = " + ".join(g["name"] for g in gpus if g.get("name"))
    state.update(
        gpus=gpus,
        gpu_name=names or None,
        gpu_util=(sum(util) / len(util)) if util else None,
        gpu_vram_used=sum(used) if used else None,
        gpu_vram_total=sum(total) if total else None,
        gpu_temp=max(temps) if temps else None)


class State:
    """Thread-safe current-state snapshot shared by collector, sampler and API."""

    def __init__(self, unit, host=None):
        self._lock = threading.RLock()
        self.d = {
            "ts": time.time(),
            "unit": unit,
            "host": host or "",
            "engine": "unknown",          # running|restarting|stopped|failed|unknown
            "stage": "idle",
            "stage_since": time.time(),
            "stale": False,
            "tool": None,
            "tok_s": 0.0,
            "tok_s_eng": None,       # engine-wide output: summed decoding slots
            "gen": None,
            "budget": None,
            "prompt": None,
            "context": None,
            "context_window": None,
            "elapsed": None,
            "started_ts": None,
            "last_line_ts": None,
            "cpu_cores": 0.0,
            "rss": 0,
            "sys_ram_pct": 0.0,
            "sys_ram_used": None,
            "sys_ram_total": None,
            "sys_load": 0.0,
            "nproc": os.cpu_count() or 1,
            "gpu_util": None,
            "gpu_vram_used": None,
            "gpu_vram_total": None,
            "gpu_temp": None,
            "gpu_name": None,
            "gpus": [],              # per-GPU live readings (multi-GPU / layer split)
            "gpu_links": {},         # gpu index -> PCIe (width, gen) from nvidia-smi
            # engine-side parallel (batch slot) state, from the /metrics poller
            "parallel": None,        # slot count N, None = never reported
            "slots": [],
            "req_running": None,
            "req_waiting": None,
            "req_queued": None,
            "slots_busy": None,
            "slots_total": None,
            "metrics_ts": None,      # last successful /metrics poll
            "last_request": None,
            "source": "journal",
            "model": None,
            "active_unit": None,
            "server_url": None,
        }

    def update(self, **kw):
        with self._lock:
            self.d.update(kw)

    def get(self, k, default=None):
        with self._lock:
            return self.d.get(k, default)

    def snapshot(self, stale_after_s=30.0):
        with self._lock:
            d = dict(self.d)
            d["gpus"] = [dict(g) for g in d["gpus"]]
            d["slots"] = [dict(s) for s in d["slots"]]
        d["stale"] = (d["stage"] in ACTIVE_STAGES
                      and d["last_line_ts"] is not None
                      and time.time() - d["last_line_ts"] > stale_after_s)
        d["age_s"] = round(time.time() - d["last_line_ts"]) if d["last_line_ts"] else None
        d["now"] = time.time()
        ctx, win = d["context"], d["context_window"]
        d["context_pct"] = (100.0 * ctx / win) if ctx and win else None
        return d


class EventPump:
    """Applies parsed events to the shared state and queues history rows."""

    def __init__(self, state, store):
        self.state = state
        self.store = store
        self._pending = []
        self._plock = threading.Lock()

    def _add(self, ts, stage, tok_s=None, gen=None, prompt=None, context=None):
        """Queue a sample row stamped with the *log* timestamp (history backfill)."""
        with self._plock:
            self._pending.append({
                "ts": int(ts), "stage": stage, "tok_s": tok_s, "gen": gen,
                "prompt": prompt, "context": context, "cpu_cores": None,
                "rss": None, "sys_ram_pct": None, "sys_load": None,
                "gpu_util": None, "gpu_vram_used": None,
                "gpu_vram_total": None})

    def drain(self):
        with self._plock:
            rows, self._pending = self._pending, []
        return rows

    def _since(self, new_stage, ts):
        """stage_since advances only on a real stage change."""
        return ts if new_stage != self.state.d.get("stage") \
            else self.state.d.get("stage_since")

    def _mfresh(self):
        """True while the /metrics poller is the authoritative live source -
        which is only the case when the engine reports batch slots. In solo
        mode the journal lines describe the one running request, exactly as
        the state machine expects, so nothing is gated."""
        if not self.state.d.get("parallel"):
            return False
        ts = self.state.d.get("metrics_ts")
        return ts is not None and time.time() - ts < METRICS_FRESH_S

    def _track_model(self, ev):
        """Keep state.model in sync with the instance a line came from."""
        m = ev.get("model")
        if not m:
            return
        au, eu = self.state.d.get("active_unit"), ev.get("unit")
        if au and eu and eu != au:
            return  # tail lines of an instance that already gave way
        if m != self.state.d.get("model"):
            self.state.update(model=m)
            self.store.set_meta("model", m)

    def handle(self, ev):
        k = ev.get("kind")
        if k != "systemd":
            self._track_model(ev)
        if k in ("prefill", "gen", "tool_complete") and self._mfresh():
            # /metrics owns the live picture: these per-request lines (whose
            # sources interleave when slots run) only keep the engine alive
            # in the eyes of staleness and backfill the history rows
            self._journal_activity(ev)
            self._add_activity_row(ev)
            return
        if k == "prefill":
            self.state.update(
                stage="prefill", stage_since=self._since("prefill", ev["ts"]),
                tool=None,
                prompt=ev["total"], context=ev["read"], gen=0,
                last_line_ts=ev["ts"], started_ts=ev["ts"],
                elapsed=ev.get("sec") or 0,
                tok_s=0.0, engine="running")
            self._add(ev["ts"], "prefill", None, 0, ev["total"], ev["read"])
        elif k == "gen":
            prompt = self.state.d.get("prompt") or 0
            # The engine's tok/s on the very first token of a turn (n == 1) is a
            # division artifact of startup latency, not a real rate — ignore it.
            reliable = ev["n"] > 1
            since = self._since(ev["stage"], ev["ts"])
            updates = dict(
                stage=ev["stage"], stage_since=since, tool=ev.get("tool"),
                gen=ev["n"], budget=ev.get("budget") or ev["max"],
                context=prompt + ev["n"], elapsed=ev["sec"],
                last_line_ts=ev["ts"], engine="running")
            if reliable:
                updates["tok_s"] = ev["tok_s"]
            if self.state.d.get("started_ts") is None:
                updates["started_ts"] = ev["ts"] - ev["sec"]
            self.state.update(**updates)
            self._add(ev["ts"], ev["stage"], ev["tok_s"] if reliable else None,
                      ev["n"], prompt or None, prompt + ev["n"])
        elif k == "tool_complete":
            prompt = self.state.d.get("prompt") or 0
            self.state.update(
                stage="idle", stage_since=self._since("idle", ev["ts"]),
                tool=None, gen=ev["n"],
                context=prompt + ev["n"], tok_s=0.0, elapsed=ev["sec"],
                last_line_ts=ev["ts"], engine="running")
            self._add(ev["ts"], "idle", 0.0, ev["n"], prompt or None,
                      prompt + ev["n"])
        elif k == "done":
            prompt = self.state.d.get("prompt") or 0
            if self._mfresh():
                # one of several parallel requests ended: keep its summary,
                # leave the live aggregate to the /metrics poller
                self.state.update(
                    last_line_ts=ev["ts"], engine="running",
                    last_request={"n": ev.get("n"), "sec": ev.get("sec"),
                                  "tok_s": ev.get("tok_s"),
                                  "reason": ev.get("reason"),
                                  "cache": ev.get("cache"),
                                  "pcie": ev.get("pcie"), "ts": ev["ts"]})
            else:
                self.state.update(
                    stage="idle", stage_since=self._since("idle", ev["ts"]),
                    tool=None, tok_s=0.0,
                    gen=ev.get("n"), context=prompt + (ev.get("n") or 0),
                    last_line_ts=ev["ts"], started_ts=None, engine="running",
                    last_request={"n": ev.get("n"), "sec": ev.get("sec"),
                                  "tok_s": ev.get("tok_s"),
                                  "reason": ev.get("reason"),
                                  "cache": ev.get("cache"),
                                  "pcie": ev.get("pcie"), "ts": ev["ts"]})
            self._add(ev["ts"], "idle", 0.0, ev.get("n"), prompt or None,
                      prompt + (ev.get("n") or 0))
        elif k == "ready":
            updates = dict(last_line_ts=ev["ts"], engine="running", tok_s=0.0)
            if self.state.d["stage"] in ("starting", "stopped", "error", "idle"):
                updates.update(stage="idle",
                               stage_since=self._since("idle", ev["ts"]))
            if ev.get("context_window"):
                updates["context_window"] = ev["context_window"]
                self.store.set_meta("context_window", ev["context_window"])
            if ev.get("server_url"):
                updates["server_url"] = ev["server_url"]
                self.store.set_meta("server_url", ev["server_url"])
            self.state.update(**updates)
            self._add(ev["ts"], "idle")
        elif k == "meta":
            if ev.get("model"):
                self.state.update(model=ev["model"])
                self.store.set_meta("model", ev["model"])
        elif k == "lifecycle":
            stage = ev["stage"]
            self.state.update(
                stage=stage, stage_since=self._since(stage, ev["ts"]),
                tool=None, tok_s=0.0,
                last_line_ts=ev["ts"],
                engine="failed" if stage == "error" else "running")
            self._add(ev["ts"], stage)
        elif k == "systemd":
            stage = self._systemd(ev)
            if stage:
                self._add(ev["ts"], stage)
        elif k == "note":
            self.state.update(last_line_ts=ev["ts"])

    def _journal_activity(self, ev):
        """A live engine log line while /metrics is fresh: it proves the engine
        is alive and feeds history rows, but must not move the live stage
        machine - with parallel slots these lines interleave across requests."""
        self.state.update(last_line_ts=ev["ts"], engine="running")

    def _add_activity_row(self, ev):
        """History row for a prefill/gen/tool_complete line, matching what the
        non-fresh branches queue."""
        k = ev.get("kind")
        if k == "prefill":
            self._add(ev["ts"], "prefill", None, 0, ev["total"], ev["read"])
        elif k == "gen":
            prompt = self.state.d.get("prompt") or 0
            reliable = ev["n"] > 1  # n==1 tok/s is a startup artifact
            self._add(ev["ts"], ev["stage"],
                      ev["tok_s"] if reliable else None, ev["n"],
                      prompt or None, prompt + ev["n"])
        elif k == "tool_complete":
            prompt = self.state.d.get("prompt") or 0
            self._add(ev["ts"], "idle", 0.0, ev["n"], prompt or None,
                      prompt + ev["n"])

    def _systemd(self, ev):
        a, ts = ev["action"], ev["ts"]
        au, eu = self.state.d.get("active_unit"), ev.get("unit")
        if a == "started":
            self.state.update(active_unit=eu, engine="running",
                              stage="starting",
                              stage_since=self._since("starting", ts),
                              tok_s=0.0, tool=None,
                              gen=None, prompt=None, context=None,
                              budget=None, elapsed=None, started_ts=None,
                              last_line_ts=ts)
            m = ev.get("model")
            if m and m != self.state.d.get("model"):
                self.state.update(model=m)
                self.store.set_meta("model", m)
            return "starting"
        if a in ("stopped", "stopping"):
            if au and eu and eu != au:
                return None  # old instance stopped after a new one took over
            self.state.update(active_unit=None, engine="stopped",
                              stage="stopped",
                              stage_since=self._since("stopped", ts),
                              tok_s=0.0, last_line_ts=ts)
            return "stopped"
        if a == "failed":
            if au and eu and eu != au:
                return None
            self.state.update(active_unit=None, engine="failed",
                              stage="stopped",
                              stage_since=self._since("stopped", ts),
                              tok_s=0.0, last_line_ts=ts)
            return "stopped"
        if a == "exited":
            status = ev.get("status") or ""
            if status and not status.startswith("0/"):
                self.state.update(engine="failed", last_line_ts=ts)
            return None
        if a == "restarting":
            self.state.update(engine="restarting", stage="starting",
                              stage_since=self._since("starting", ts),
                              last_line_ts=ts)
            return "starting"
        return None


class Collector:
    """Follows the journal (or a file) and samples CPU/GPU metrics."""

    def __init__(self, cfg, state, store, pump):
        self.cfg = cfg
        self.state = state
        self.store = store
        self.pump = pump
        # unbounded: journal preloads must not lose events (backpressure instead)
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._gpu_next = 0.0
        self._gpu_ok = None
        self._pcie_ok = None   # nvidia-smi knows the PCIe link fields?
        self._prev_cpu = {"ts": None, "ct": None}
        self._pid_cache = {"pids": [], "until": 0.0}
        self._poller = None

    def start(self):
        for fn, name in ((self._reader_loop, "reader"),
                         (self._worker_loop, "worker"),
                         (self._flusher_loop, "flusher"),
                         (self._sampler_loop, "sampler")):
            threading.Thread(target=fn, name=name, daemon=True).start()
        if self.cfg.get("metrics_poll", True):
            self._poller = MetricsPoller(self.cfg, self.state, self._stop)
            self._poller.start()

    def stop(self):
        self._stop.set()

    # ---- log source ---------------------------------------------------
    def _reader_loop(self):
        log_file = self.cfg.get("log_file")
        if log_file:
            self.state.update(source="file:" + log_file)
            self._read_file(log_file)
        else:
            unit = _journal_pattern(self.cfg.get("unit", "strata.service"))
            self.state.update(source="journal:" + unit)
            self._read_journal()

    def _read_file(self, path):
        from_start = self.cfg.get("file_position", "end") == "start"
        while not self._stop.is_set():
            try:
                f = open(path, "r", errors="replace")
                if not from_start:
                    f.seek(0, os.SEEK_END)
            except OSError as e:
                log.error("cannot open %s: %s", path, e)
                time.sleep(5)
                continue
            with f:
                while not self._stop.is_set():
                    line = f.readline()
                    if line:
                        self._emit(line)
                        continue
                    try:
                        if os.path.getsize(path) < f.tell():
                            break  # rotated/truncated: reopen
                    except OSError:
                        break
                    time.sleep(0.3)
            time.sleep(1)

    def _read_journal(self):
        unit = _journal_pattern(self.cfg.get("unit", "strata.service"))
        if not shutil.which("journalctl"):
            log.error("journalctl not found and no log_file configured")
            while not self._stop.is_set():
                time.sleep(30)
            return
        preload = int(self.cfg.get("preload_hours", 24))
        while not self._stop.is_set():
            if preload > 0:
                log.info("preloading the last %sh of %s", preload, unit)
                self._run(["journalctl", "-u", unit, "--since",
                           "-%d hours" % preload, "-o", "short-iso",
                           "--no-pager"], follow=False)
                preload = 0
            ok = self._run(["journalctl", "-u", unit, "-f", "-n", "0",
                            "-o", "short-iso", "--no-pager"], follow=True)
            if not ok:
                log.error("journalctl follow failed; retrying in 10 s")
                time.sleep(10)

    def _run(self, cmd, follow):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            log.error("spawn %s failed: %s", cmd[0], e)
            return False
        try:
            for line in p.stdout:
                if self._stop.is_set():
                    break
                self._emit(line)
            err = p.stderr.read() if follow else ""
        except ValueError:
            err = ""
        finally:
            if follow:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if err and "Hint:" not in err:
            log.warning("journalctl: %s", err.strip()[:300])
        # a globbed -u pattern that matches no unit in the journal yet makes
        # journalctl exit non-zero; report it so the caller backs off instead
        # of respawning it in a tight loop
        return p.returncode == 0

    def _emit(self, line):
        line = line.rstrip("\n")
        if not line:
            return
        ev = parse_line(line, unit=self.cfg.get("unit", "strata.service"))
        if ev is None:
            return
        self._events.put(ev)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                ev = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.pump.handle(ev)
            except Exception:
                log.exception("event handling failed")

    def _flusher_loop(self):
        """Writes backfilled event rows (real log timestamps) in batches."""
        try:
            while not self._stop.is_set():
                rows = self.pump.drain()
                if rows:
                    self.store.insert_samples(rows)
                time.sleep(0.5)
        finally:
            rows = self.pump.drain()
            if rows:
                try:
                    self.store.insert_samples(rows)
                except Exception:
                    log.exception("final flush failed")

    # ---- metrics ------------------------------------------------------
    def _sampler_loop(self):
        interval = float(self.cfg.get("sample_interval_s", 1.0))
        idle_interval = float(self.cfg.get("idle_interval_s", 10.0))
        stale_after = float(self.cfg.get("stale_after_s", 30))
        retention = int(self.cfg.get("retention_days", 14))
        last_insert = 0.0
        last_prune = time.time()
        while not self._stop.is_set():
            t0 = time.time()
            self._sample()
            st = self.state.snapshot(stale_after)
            active = st["stage"] in ACTIVE_STAGES or (st["cpu_cores"] or 0) > 0.1
            if t0 - last_insert >= (interval if active else idle_interval):
                row = self._row(st)
                self.store.insert_sample(row)
                self.store.insert_gpu_samples(row["ts"], st["gpus"])
                last_insert = t0
            if t0 - last_prune > 3600:
                self.store.prune(retention)
                last_prune = t0
            time.sleep(max(0.2, interval - (time.time() - t0)))

    def _row(self, st):
        active = st["stage"] in ACTIVE_STAGES
        # while parallel slots decode, the engine-wide speed is the honest
        # "output speed" even if the tracked request itself is prefilling
        eng = st["tok_s_eng"]
        return {"ts": int(time.time()), "stage": st["stage"],
                "tok_s": eng if eng is not None
                else (st["tok_s"] if active else 0.0),
                "gen": st["gen"], "prompt": st["prompt"],
                "context": st["context"], "cpu_cores": st["cpu_cores"],
                "rss": st["rss"], "sys_ram_pct": st["sys_ram_pct"],
                "sys_load": st["sys_load"], "gpu_util": st["gpu_util"],
                "gpu_vram_used": st["gpu_vram_used"],
                "gpu_vram_total": st["gpu_vram_total"],
                "req_running": st["req_running"],
                "req_waiting": st["req_waiting"],
                "slots_busy": st["slots_busy"],
                "slots_total": st["slots_total"]}

    def _sample(self):
        now = time.time()
        self._sample_cpu(now)
        self._sample_system()
        if now >= self._gpu_next:
            self._gpu_next = now + float(self.cfg.get("gpu_interval_s", 3.0))
            self._sample_gpu()
        self.state.update(ts=now)

    def _strata_pids(self, now):
        if now < self._pid_cache["until"]:
            return self._pid_cache["pids"]
        pids = []
        unit = self.state.d.get("active_unit")
        if not unit and "@." not in self.cfg.get("unit", "strata.service"):
            unit = self.cfg.get("unit", "strata.service")
        if unit:
            try:
                r = subprocess.run(["systemctl", "show", "-p", "MainPID",
                                    "--value", unit],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    pid = int(r.stdout.strip() or 0)
                    if pid > 1 and psutil.pid_exists(pid):
                        pids.append(pid)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if not pids:
            me = os.getpid()
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cl = " ".join(p.info["cmdline"] or [])
                    if ("strata" in cl and "strata-monitor" not in cl
                            and "journalctl" not in cl
                            and p.info["pid"] != me):
                        pids.append(p.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        self._pid_cache = {"pids": pids, "until": now + 5.0}
        return pids

    @staticmethod
    def _proc_cpu(p):
        try:
            t = p.cpu_times()
            return t.user + t.system + getattr(t, "children_user", 0) \
                + getattr(t, "children_system", 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return 0.0

    def _sample_cpu(self, now):
        cores, rss, total = 0.0, 0, 0.0
        procs = []
        for pid in self._strata_pids(now):
            try:
                p = psutil.Process(pid)
                procs.append(p)
                rss += p.memory_info().rss
                total += self._proc_cpu(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        prev = self._prev_cpu
        if procs and prev["ts"] and now > prev["ts"]:
            cores = max(0.0, (total - prev["ct"]) / (now - prev["ts"]))
            cores = min(cores, float(os.cpu_count() or 1))
        self._prev_cpu = {"ts": now, "ct": total} if procs else {"ts": None, "ct": None}
        self.state.update(cpu_cores=round(cores, 2), rss=rss)

    def _sample_system(self):
        vm = psutil.virtual_memory()
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = 0.0
        self.state.update(sys_ram_pct=vm.percent, sys_ram_used=vm.used,
                          sys_ram_total=vm.total, sys_load=round(load, 2))

    GPU_Q = ("--query-gpu=index,name,utilization.gpu,memory.used,"
             "memory.total,temperature.gpu")

    def _sample_gpu(self):
        if not shutil.which("nvidia-smi"):
            if self._gpu_ok is not False:
                self._gpu_ok = False
                log.info("nvidia-smi not found; GPU readings come from the "
                         "engine's /metrics only")
            return
        pcie = self._pcie_ok is not False
        q = self.GPU_Q + (",pcie.link.width.current,pcie.link.gen.current"
                          if pcie else "")
        try:
            r = subprocess.run(["nvidia-smi", q,
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=4)
            if r.returncode != 0:
                if pcie and "field" in (r.stderr or "").lower():
                    self._pcie_ok = False  # too old to know the link fields
                    log.info("this nvidia-smi has no PCIe link fields; "
                             "dropping them")
                    return self._sample_gpu()
                raise RuntimeError(r.stderr.strip()[:200] or "bad output")
            gpus = parse_nvidia_smi(r.stdout, 6 if pcie else 4)
            if not gpus:
                raise RuntimeError("no GPU rows in nvidia-smi output")
            lk = {g["index"]: (g["link_width"], g["link_gen"])
                  for g in gpus if g.get("link_width") is not None}
            if lk:
                self.state.update(gpu_links=lk)
            apply_gpus(self.state, gpus)
            self._gpu_ok = True
        except Exception as e:
            log.warning("nvidia-smi failed: %s", e)


# the engine's live.phase strings -> our stage names
PHASE_STAGES = (("thinking", "thinking"),
                ("answering", "answering"),
                ("writing a tool call", "tool_call"),
                ("tool call complete", "idle"))


def phase_stage(phase, default="answering"):
    p = (phase or "").lower()
    for needle, stage in PHASE_STAGES:
        if p.startswith(needle) or needle in p:
            return stage
    return default


class MetricsPoller:
    """Polls the strata server's own /metrics endpoint.

    The engine's log lines cannot be used for the live state when it runs in
    parallel mode ("parallel": N): the progress lines of the several requests
    interleave with no slot id. /metrics carries the authoritative picture -
    live.running/waiting/admitting, live.slots[] per batch slot, and each
    card's readings in hardware (which also covers AMD cards, and a layer
    split's several cards at once). While this data is fresh, the pump lets
    it own the live stage machine (see EventPump._mfresh).
    """

    def __init__(self, cfg, state, stop):
        self.cfg = cfg
        self.state = state
        self._stop = stop
        self._err = None            # last failure reason; logged once
        self._fails = 0             # consecutive failures since the last success
        self._notified_no_url = False

    def start(self):
        threading.Thread(target=self._loop, name="metrics",
                         daemon=True).start()

    def _url(self):
        base = self.cfg.get("metrics_url") or self.state.d.get("server_url")
        if not base:
            return None
        base = str(base).rstrip("/")
        return base if base.endswith("/metrics") else base + "/metrics"

    def _cadence(self):
        """Poll fast until the first success (so a URL learned from the ready:
        line is picked up promptly), then on the sampler's active/idle tempo;
        sustained failures fall back to the slow tempo so an engine without
        /metrics is not hammered."""
        hi = float(self.cfg.get("sample_interval_s", 1.0))
        lo = float(self.cfg.get("idle_interval_s", 10.0))
        if self.state.d.get("metrics_ts") is None and self._fails < 5:
            return hi
        return hi if self.state.d.get("stage") in ACTIVE_STAGES else lo

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            self._poll()
            time.sleep(max(0.2, self._cadence() - (time.time() - t0)))

    def _poll(self):
        url = self._url()
        if url is None:
            if not self._notified_no_url:
                log.info("engine server URL not seen yet (ready: line or "
                         "metrics_url); parallel state stays journal-only")
                self._notified_no_url = True
            return
        req = urllib.request.Request(url)
        key = self.cfg.get("api_key")
        if key:
            req.add_header("Authorization", "Bearer %s" % key)
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._fails += 1
            reason = str(getattr(e, "reason", None) or e)
            if reason != self._err:
                log.warning("engine /metrics failed: %s", reason)
                self._err = reason
            return
        if self._err is not None:
            log.info("engine /metrics reachable again")
            self._err = None
        self._fails = 0
        try:
            self._apply(data)
        except Exception:
            log.exception("engine /metrics parse failed")

    # ---- parsing ------------------------------------------------------
    @staticmethod
    def _hardware_gpus(data):
        """hardware.gpus[] (layer split, strata 0.1.44+) or the single-card
        gpu_* scalars; the engine reports bytes, nvidia-smi-style state MiB."""
        def mib(v):
            return int(v / 1048576) if isinstance(v, (int, float)) else None
        raw = data.get("hardware") or {}
        static = data.get("hardware_static") or {}
        gpus = raw.get("gpus")
        if isinstance(gpus, list) and gpus:
            out = []
            for g in gpus:
                if not isinstance(g, dict):
                    continue
                out.append({"index": g.get("index"),
                            "name": g.get("name"),
                            "util": g.get("util"),
                            "vram_used": mib(g.get("mem_used")),
                            "vram_total": mib(g.get("mem_total")),
                            "temp": g.get("temp")})
            return out
        if raw.get("gpu_mem_total") is None and raw.get("gpu_util") is None:
            return []
        return [{"index": 0, "name": static.get("gpu_name") or "GPU",
                 "util": raw.get("gpu_util"),
                 "vram_used": mib(raw.get("gpu_mem_used")),
                 "vram_total": mib(raw.get("gpu_mem_total")),
                 "temp": raw.get("gpu_temp")}]

    def _apply(self, data):
        live = data.get("live") or {}
        now = time.time()
        par = int(live.get("parallel") or 0)
        slots = [s for s in (live.get("slots") or []) if isinstance(s, dict)]
        busy = [s for s in slots if s.get("state") != "idle"]
        running = live.get("running")
        u = {"metrics_ts": now,
             "parallel": par or None,
             "slots": slots,
             "req_running": running,
             "req_waiting": live.get("waiting"),
             "req_queued": live.get("queued"),
             "slots_busy": len(busy) if slots else None,
             "slots_total": len(slots) if slots or par else None,
             # engine-wide output speed: summed tok/s of decoding slots; None
             # when nothing decodes inside slots (solo path keeps journal tok_s)
             "tok_s_eng": sum((x.get("tok_s") or 0.0) for x in busy) or None}

        if par:
            # parallel mode: with fresh /metrics data this owns the live
            # stage machine; the journal's progress lines are per-request
            # and interleaved, the slots are not
            if running and (live.get("state") == "reading"
                            or any(s.get("state") == "reading" for s in busy)):
                stage = "prefill"
            elif running:
                stage = phase_stage(live.get("phase"))
            else:
                stage = "idle"
            prev = self.state.d.get("stage")
            u["stage"] = stage
            u["stage_since"] = (now if stage != prev
                                else self.state.d.get("stage_since"))
            if stage == "prefill":
                src = busy or [live]
                prompt = max((x.get("prompt_tokens") or 0) for x in src)
                read = max((x.get("prompt_read") or 0) for x in src)
                u.update(prompt=prompt or None, context=read or None, gen=0,
                         tok_s=0.0, engine="running")
                u["elapsed"] = max((x.get("elapsed_s") or 0) for x in src)
            elif stage in ACTIVE_STAGES:
                src = busy or [live]
                gen = sum((x.get("generated") or 0) for x in busy) \
                    if busy else live.get("generated")
                prompt = max((x.get("prompt_tokens") or 0) for x in src) \
                    or live.get("prompt_tokens")
                tok = sum((x.get("tok_s") or 0.0) for x in busy) if busy \
                    else live.get("tok_s")
                elapsed = max((x.get("elapsed_s") or 0) for x in src) \
                    or live.get("elapsed_s")
                phase = live.get("phase") or ""
                tool = (phase.split(": ", 1)[1]
                        if phase.lower().startswith("writing a tool call")
                        and ": " in phase else None)
                u.update(gen=gen,
                         prompt=prompt or None,
                         context=(prompt + gen) if prompt and gen is not None
                         else None,
                         tok_s=tok or None, elapsed=elapsed,
                         started_ts=(now - elapsed) if elapsed else None,
                         tool=tool, engine="running")
            else:
                u.update(tok_s=0.0, tool=None, engine="running")

        gpus = self._hardware_gpus(data)
        if gpus:
            apply_gpus(self.state, gpus)
        self.state.update(**u)
