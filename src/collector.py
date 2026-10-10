"""Collects strata events from the journal (or a log file) and system metrics."""

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

import psutil

from parser import parse_line
from store import STAGE_ID

log = logging.getLogger("collector")

ACTIVE_STAGES = {"prefill", "thinking", "answering", "tool_call"}


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
            "sys_load": 0.0,
            "nproc": os.cpu_count() or 1,
            "gpu_util": None,
            "gpu_vram_used": None,
            "gpu_vram_total": None,
            "gpu_temp": None,
            "gpu_name": None,
            "last_request": None,
            "source": "journal",
            "model": None,
            "server_url": None,
        }

    def update(self, **kw):
        with self._lock:
            self.d.update(kw)

    def snapshot(self, stale_after_s=30.0):
        with self._lock:
            d = dict(self.d)
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

    def handle(self, ev):
        k = ev.get("kind")
        if k == "prefill":
            self.state.update(
                stage="prefill", stage_since=ev["ts"], tool=None,
                prompt=ev["total"], context=ev["read"], gen=0,
                last_line_ts=ev["ts"], started_ts=ev["ts"], elapsed=0,
                tok_s=0.0, engine="running")
            self._add(ev["ts"], "prefill", None, 0, ev["total"], ev["read"])
        elif k == "gen":
            prompt = self.state.d.get("prompt") or 0
            updates = dict(
                stage=ev["stage"], stage_since=ev["ts"], tool=ev.get("tool"),
                gen=ev["n"], budget=ev["max"], tok_s=ev["tok_s"],
                context=prompt + ev["n"], elapsed=ev["sec"],
                last_line_ts=ev["ts"], engine="running")
            if self.state.d.get("started_ts") is None:
                updates["started_ts"] = ev["ts"] - ev["sec"]
            self.state.update(**updates)
            self._add(ev["ts"], ev["stage"], ev["tok_s"], ev["n"],
                      prompt or None, prompt + ev["n"])
        elif k == "tool_complete":
            prompt = self.state.d.get("prompt") or 0
            self.state.update(
                stage="idle", stage_since=ev["ts"], tool=None, gen=ev["n"],
                context=prompt + ev["n"], tok_s=0.0, elapsed=ev["sec"],
                last_line_ts=ev["ts"], engine="running")
            self._add(ev["ts"], "idle", 0.0, ev["n"], prompt or None,
                      prompt + ev["n"])
        elif k == "done":
            prompt = self.state.d.get("prompt") or 0
            self.state.update(
                stage="idle", stage_since=ev["ts"], tool=None, tok_s=0.0,
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
                updates.update(stage="idle", stage_since=ev["ts"])
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
                stage=stage, stage_since=ev["ts"], tool=None, tok_s=0.0,
                last_line_ts=ev["ts"],
                engine="failed" if stage == "error" else "running")
            self._add(ev["ts"], stage)
        elif k == "systemd":
            stage = self._systemd(ev)
            if stage:
                self._add(ev["ts"], stage)
        elif k == "note":
            self.state.update(last_line_ts=ev["ts"])

    def _systemd(self, ev):
        a, ts = ev["action"], ev["ts"]
        if a == "started":
            self.state.update(engine="running", stage="starting",
                              stage_since=ts, tok_s=0.0, tool=None,
                              gen=None, prompt=None, context=None,
                              budget=None, elapsed=None, started_ts=None,
                              last_line_ts=ts)
            return "starting"
        if a in ("stopped", "stopping"):
            self.state.update(engine="stopped", stage="stopped",
                              stage_since=ts, tok_s=0.0, last_line_ts=ts)
            return "stopped"
        if a == "failed":
            self.state.update(engine="failed", stage="stopped",
                              stage_since=ts, tok_s=0.0, last_line_ts=ts)
            return "stopped"
        if a == "exited":
            status = ev.get("status") or ""
            if status and not status.startswith("0/"):
                self.state.update(engine="failed", last_line_ts=ts)
            return None
        if a == "restarting":
            self.state.update(engine="restarting", stage="starting",
                              stage_since=ts, last_line_ts=ts)
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
        self._prev_cpu = {"ts": None, "ct": None}
        self._pid_cache = {"pids": [], "until": 0.0}

    def start(self):
        for fn, name in ((self._reader_loop, "reader"),
                         (self._worker_loop, "worker"),
                         (self._flusher_loop, "flusher"),
                         (self._sampler_loop, "sampler")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ---- log source ---------------------------------------------------
    def _reader_loop(self):
        log_file = self.cfg.get("log_file")
        if log_file:
            self.state.update(source="file:" + log_file)
            self._read_file(log_file)
        else:
            unit = self.cfg.get("unit", "strata.service")
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
        unit = self.cfg.get("unit", "strata.service")
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
        if err and "Hint:" not in err:
            log.warning("journalctl: %s", err.strip()[:300])
        return True

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
                self.store.insert_sample(self._row(st))
                last_insert = t0
            if t0 - last_prune > 3600:
                self.store.prune(retention)
                last_prune = t0
            time.sleep(max(0.2, interval - (time.time() - t0)))

    def _row(self, st):
        active = st["stage"] in ACTIVE_STAGES
        return {"ts": int(time.time()), "stage": st["stage"],
                "tok_s": st["tok_s"] if active else 0.0,
                "gen": st["gen"], "prompt": st["prompt"],
                "context": st["context"], "cpu_cores": st["cpu_cores"],
                "rss": st["rss"], "sys_ram_pct": st["sys_ram_pct"],
                "sys_load": st["sys_load"], "gpu_util": st["gpu_util"],
                "gpu_vram_used": st["gpu_vram_used"],
                "gpu_vram_total": st["gpu_vram_total"]}

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
        unit = self.cfg.get("unit", "strata.service")
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
        self.state.update(sys_ram_pct=vm.percent, sys_load=round(load, 2))

    def _sample_gpu(self):
        if not shutil.which("nvidia-smi"):
            if self._gpu_ok is not False:
                self._gpu_ok = False
                log.info("nvidia-smi not found; the GPU panel stays empty")
            return
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4)
            parts = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            if r.returncode != 0 or len(parts) < 5:
                raise RuntimeError(r.stderr.strip()[:200] or "bad output")
            self.state.update(gpu_name=parts[0], gpu_util=float(parts[1]),
                              gpu_vram_used=int(parts[2]),
                              gpu_vram_total=int(parts[3]),
                              gpu_temp=float(parts[4]))
            self._gpu_ok = True
        except Exception as e:
            log.warning("nvidia-smi failed: %s", e)
