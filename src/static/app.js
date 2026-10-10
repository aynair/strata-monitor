/* Strata monitor dashboard (hand-rolled canvas plots, no external assets) */
"use strict";

const $ = (id) => document.getElementById(id);

const STAGES = {
  idle:      { c: "#64748b", l: "idle" },
  starting:  { c: "#38bdf8", l: "starting" },
  prefill:   { c: "#22d3ee", l: "reading prompt" },
  thinking:  { c: "#a78bfa", l: "thinking" },
  answering: { c: "#34d399", l: "answering" },
  tool_call: { c: "#fbbf24", l: "tool call" },
  stopped:   { c: "#94a3b8", l: "stopped" },
  error:     { c: "#f87171", l: "error" },
};
const ACTIVE = new Set(["prefill", "thinking", "answering", "tool_call"]);

const fmtInt = (n) => (n === null || n === undefined ? "–"
  : Number(n).toLocaleString("en-US"));
const fmt1 = (n) => (n === null || n === undefined ? "–" : Number(n).toFixed(1));
const fmt2 = (n) => (n === null || n === undefined ? "–" : Number(n).toFixed(2));
const esc = (s) => String(s).replace(/[&<>]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function hhmmss(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0, 8);
}
function hhmm(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0, 5);
}
function mmddhhmm(t) {
  const d = new Date(t * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function xLabel(t, span) {
  return span > 2 * 86400 ? mmddhhmm(t) : span > 7200 ? hhmm(t) : hhmmss(t);
}
function dur(sec) {
  if (sec === null || sec === undefined) return "–";
  sec = Math.round(sec);
  if (sec < 90) return `${sec} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return `${h} h ${m} m`;
}

/* ------------------------------------------------------------------ */
/* canvas plot                                                         */
function niceScale(lo, hi, target = 4) {
  if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
  if (lo === hi) { hi = lo + 1; }
  const span = hi - lo;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  return { min: Math.floor(lo / step) * step, max: Math.ceil(hi / step) * step, step };
}

class Plot {
  constructor(id, opts) {
    this.root = $(id);
    this.canvas = this.root.querySelector("canvas");
    this.tip = this.root.querySelector(".tip");
    this.ctx = this.canvas.getContext("2d");
    this.o = Object.assign({
      color: "#4fd1c5", ymin: null, ymax: null, bands: true,
      fmt: (v) => fmt1(v), tipFmt: (v) => fmt1(v), unit: "",
    }, opts);
    this.data = { ts: [], v: [], stages: [] };
    this.hover = -1;
    this.pad = { l: 50, r: 12, t: 10, b: 22 };

    this.canvas.addEventListener("mousemove", (e) => this._onMove(e));
    this.canvas.addEventListener("mouseleave", () => {
      this.hover = -1; this.tip.style.display = "none"; this.draw();
    });
    new ResizeObserver(() => this.resize()).observe(this.root);
    this.resize();
  }

  setData(ts, v, stages) {
    this.data = { ts: ts || [], v: v || [], stages: stages || [] };
    this.hover = -1;
    this.tip.style.display = "none";
    this.draw();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
    this.draw();
  }

  _range() {
    const o = this.o;
    let lo = o.ymin, hi = o.ymax;
    const vals = this.data.v.filter((x) => x !== null && x !== undefined && isFinite(x));
    if (!vals.length) return null;
    const dlo = Math.min(...vals), dhi = Math.max(...vals);
    if (lo === null || lo === undefined) lo = Math.min(dlo, 0);
    if (hi === null || hi === undefined) hi = dhi + (dhi - lo) * 0.12 || 1;
    if (hi <= lo) hi = lo + 1;
    return niceScale(lo, hi);
  }

  _geom() {
    const { l, r, t, b } = this.pad;
    return { x0: l, x1: this.w - r, y0: t, y1: this.h - b };
  }

  _onMove(e) {
    const ts = this.data.ts;
    if (!ts.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const g = this._geom();
    const t0 = ts[0], t1 = ts[ts.length - 1] || ts[0] + 1;
    const frac = Math.min(1, Math.max(0, (mx - g.x0) / Math.max(1, g.x1 - g.x0)));
    const t = t0 + frac * (t1 - t0);
    let best = 0, bd = Infinity;
    for (let i = 0; i < ts.length; i++) {
      const d = Math.abs(ts[i] - t);
      if (d < bd) { bd = d; best = i; }
    }
    this.hover = best;
    this.draw();
    this._showTip(best, g);
  }

  _showTip(i, g) {
    const v = this.data.v[i], st = this.data.stages[i];
    const meta = STAGES[st];
    const val = (v === null || v === undefined) ? "–"
      : this.o.tipFmt(v) + (this.o.unit ? " " + this.o.unit : "");
    this.tip.innerHTML =
      `<span class="t">${hhmmss(this.data.ts[i])}</span><br>` +
      `${val}` +
      (meta ? `<span class="s" style="color:${meta.c}">${meta.l}</span>` : "");
    this.tip.style.display = "block";
    const x = this._x(i, g);
    const tw = this.tip.offsetWidth;
    this.tip.style.left = (x + 12 + tw > this.w) ? Math.max(0, x - tw - 12) + "px"
                                                 : (x + 12) + "px";
    this.tip.style.top = (g.y0 + 4) + "px";
  }

  _x(i, g) {
    const ts = this.data.ts;
    const t0 = ts[0], t1 = ts[ts.length - 1] || ts[0] + 1;
    if (t1 <= t0) return (g.x0 + g.x1) / 2;
    return g.x0 + (ts[i] - t0) / (t1 - t0) * (g.x1 - g.x0);
  }

  draw() {
    const ctx = this.ctx;
    if (!this.w || !this.h) return;
    const g = this._geom();
    ctx.clearRect(0, 0, this.w, this.h);
    const ts = this.data.ts;

    if (!ts.length) {
      ctx.fillStyle = "#7f8ea8";
      ctx.font = "12px ui-monospace, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.fillText("no data yet", this.w / 2, this.h / 2);
      ctx.textAlign = "left";
      return;
    }
    const scale = this._range();
    if (!scale) return;
    const yOf = (v) => g.y1 - (v - scale.min) / (scale.max - scale.min) * (g.y1 - g.y0);
    const t0 = ts[0], t1 = ts[ts.length - 1] || ts[0] + 1;
    const xOf = (t) => t1 <= t0 ? (g.x0 + g.x1) / 2
      : g.x0 + (t - t0) / (t1 - t0) * (g.x1 - g.x0);

    // stage bands
    if (this.o.bands && this.data.stages.length) {
      const st = this.data.stages;
      let i = 0;
      while (i < st.length) {
        let j = i;
        while (j + 1 < st.length && st[j + 1] === st[i]) j++;
        const meta = STAGES[st[i]];
        if (meta && st[i] !== "idle" && st[i] !== "stopped") {
          const xa = i === 0 ? g.x0 : xOf(ts[i]);
          const xb = j >= st.length - 1 ? g.x1 : xOf(ts[j]);
          ctx.fillStyle = meta.c + "22";
          ctx.fillRect(xa, g.y0, Math.max(1, xb - xa), g.y1 - g.y0);
        }
        i = j + 1;
      }
    }

    // grid + y labels
    ctx.font = "10px ui-monospace, Menlo, monospace";
    ctx.textBaseline = "middle";
    for (let v = scale.min; v <= scale.max + 1e-9; v += scale.step) {
      const y = yOf(v);
      ctx.strokeStyle = "#1a2438";
      ctx.beginPath(); ctx.moveTo(g.x0, y); ctx.lineTo(g.x1, y); ctx.stroke();
      ctx.fillStyle = "#7f8ea8";
      ctx.textAlign = "right";
      ctx.fillText(this.o.fmt(v), g.x0 - 7, y);
    }
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    // x labels
    const span = t1 - t0;
    const ticks = 5;
    for (let k = 0; k <= ticks; k++) {
      const t = t0 + span * (k / ticks);
      const x = xOf(t);
      ctx.fillStyle = "#7f8ea8";
      ctx.textAlign = k === 0 ? "left" : k === ticks ? "right" : "center";
      ctx.fillText(xLabel(t, span), x, this.h - 6);
    }
    ctx.textAlign = "left";

    // line
    ctx.strokeStyle = this.o.color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < ts.length; i++) {
      const v = this.data.v[i];
      if (v === null || v === undefined || !isFinite(v)) { pen = false; continue; }
      const x = xOf(ts[i]), y = yOf(v);
      if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
    }
    ctx.stroke();

    // hover
    if (this.hover >= 0 && this.hover < ts.length) {
      const v = this.data.v[this.hover];
      const x = xOf(ts[this.hover]);
      ctx.strokeStyle = "#33415588";
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, g.y0); ctx.lineTo(x, g.y1); ctx.stroke();
      ctx.setLineDash([]);
      if (v !== null && v !== undefined && isFinite(v)) {
        ctx.fillStyle = this.o.color;
        ctx.beginPath(); ctx.arc(x, yOf(v), 3.2, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = "#0b1220";
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, yOf(v), 3.2, 0, Math.PI * 2); ctx.stroke();
      }
    }
  }
}

/* ------------------------------------------------------------------ */
const plots = {};
let win = 3600;
let STATE = null;
let lastGoodHistory = 0;

function initPlots() {
  plots.toks = new Plot("p-toks", {
    color: "#4fd1c5", unit: "tok/s",
    fmt: (v) => fmt1(v), tipFmt: (v) => fmt1(v),
  });
  plots.cpu = new Plot("p-cpu", {
    color: "#60a5fa", unit: "cores",
    fmt: (v) => fmt1(v), tipFmt: (v) => fmt2(v),
  });
  plots.ctx = new Plot("p-ctx", {
    color: "#a78bfa", unit: "%",
    fmt: (v) => fmtInt(v), tipFmt: (v) => fmt1(v),
  });
  plots.gpu = new Plot("p-gpu", {
    color: "#34d399", unit: "%", ymin: 0, ymax: 100,
    fmt: (v) => fmtInt(v), tipFmt: (v) => fmtInt(v),
  });
  plots.vram = new Plot("p-vram", {
    color: "#fbbf24", unit: "GiB", ymin: 0,
    fmt: (v) => fmt1(v), tipFmt: (v) => fmt2(v),
  });
  plots.ram = new Plot("p-ram", {
    color: "#f472b6", unit: "%", ymin: 0, ymax: 100,
    fmt: (v) => fmtInt(v), tipFmt: (v) => fmt1(v),
  });

  const legend = $("legend");
  legend.innerHTML = Object.keys(STAGES)
    .filter((k) => k !== "stopped" && k !== "error")
    .map((k) => `<span><i style="background:${STAGES[k].c}"></i>${STAGES[k].l}</span>`)
    .join("");
}

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

/* ------------------------------------------------------------------ */
function renderState(s) {
  STATE = s;
  const eng = $("engine");
  const engMap = {
    running: ["ok", "engine running"],
    restarting: ["warn", "engine restarting"],
    stopped: ["mute", "engine stopped"],
    failed: ["bad", "engine failed"],
    unknown: ["dim", "engine ?"],
  };
  const [cls, txt] = engMap[s.engine] || engMap.unknown;
  eng.className = "pill " + cls;
  eng.textContent = txt;

  $("host").textContent = s.host || "";
  $("model").textContent = s.model ? "model " + s.model : "";
  $("server-url").textContent = s.server_url || "";

  // stage
  const meta = STAGES[s.stage] || STAGES.idle;
  const active = ACTIVE.has(s.stage) && !s.stale;
  $("stage-badge").textContent = meta.l;
  $("stage-badge").style.color = meta.c;
  const dot = $("stage-dot");
  dot.style.background = meta.c;
  dot.style.boxShadow = `0 0 0 3px ${meta.c}33`;
  dot.classList.toggle("live", active);
  let sub = `for ${dur(s.now - s.stage_since)}`;
  if (s.tool) sub += ` · tool: ${s.tool}`;
  if (s.stale) sub += ` · no log for ${dur(s.age_s)}`;
  else if (s.age_s !== null && s.age_s > 2 && !ACTIVE.has(s.stage))
    sub += ` · last log ${dur(s.age_s)} ago`;
  $("stage-sub").textContent = sub;

  // speed
  $("toks").textContent = fmt1(s.tok_s);
  let sp = "idle";
  if (ACTIVE.has(s.stage)) {
    sp = `gen ${fmtInt(s.gen)} tok · ${dur(s.elapsed)}`;
    if (s.budget) sp += ` · budget ${fmtInt(s.budget)}`;
  } else if (s.last_request && s.last_request.tok_s != null && s.stage === "idle") {
    sp = `last request avg ${fmt1(s.last_request.tok_s)} tok/s`;
  }
  $("toks-sub").textContent = sp;

  // cpu
  $("cpu").textContent = fmt1(s.cpu_cores);
  $("cpu-total").textContent = `/ ${s.nproc} cores`;
  const rss = s.rss ? (s.rss / 1073741824) : 0;
  $("cpu-sub").textContent =
    `strata RSS ${fmt1(rss)} GiB · system RAM ${fmt1(s.sys_ram_pct)}% · load ${fmt2(s.sys_load)}`;

  // context
  const ctxWin = s.context_window;
  const pct = s.context_pct;
  $("ctx-pct").textContent = pct !== null && pct !== undefined
    ? fmt1(pct) + "%" : "–";
  $("ctx-frac").textContent = s.context != null
    ? `${fmtInt(s.context)} / ${fmtInt(ctxWin)} tok` : "";
  $("ctx-bar").style.width = (pct ? Math.min(100, pct) : 0) + "%";
  let cs = "no request yet";
  if (s.context != null) {
    cs = `prompt ${fmtInt(s.prompt)} · generated ${fmtInt(s.gen)}`;
    cs += ACTIVE.has(s.stage) ? " · request running" : " · last request";
  }
  $("ctx-sub").textContent = cs;

  // gpu
  if (s.gpu_util === null || s.gpu_util === undefined) {
    $("gpu-util").textContent = "–";
    $("gpu-sub").textContent = "nvidia-smi unavailable";
  } else {
    $("gpu-util").textContent = fmtInt(s.gpu_util);
    let g = `VRAM ${fmt1((s.gpu_vram_used || 0) / 1024)} / ` +
      `${fmt1((s.gpu_vram_total || 0) / 1024)} GiB`;
    if (s.gpu_temp != null) g += ` · ${fmtInt(s.gpu_temp)} °C`;
    if (s.gpu_name) g += ` · ${s.gpu_name}`;
    $("gpu-sub").textContent = g;
  }

  // last request
  const lr = s.last_request;
  if (lr && lr.tok_s != null) {
    $("lr-toks").textContent = fmt1(lr.tok_s);
    let line = `${fmtInt(lr.n)} tok · ${dur(lr.sec)} · ${esc(lr.reason || "?")}`;
    if (lr.cache != null) {
      line += `<br>cache hit ${fmt1(lr.cache)}%` +
        (lr.pcie != null ? ` (+${fmt1(lr.pcie)}% PCIe)` : "");
    }
    $("lr-sub").innerHTML = line;
  } else {
    $("lr-toks").textContent = "–";
    $("lr-sub").textContent = "no completed request yet";
  }

  $("src").textContent = `source ${s.source} · ${fmtInt(s.samples)} samples · ` +
    `retention ${s.history_retention_days} d`;
  $("updated").textContent = "updated " + hhmmss(s.now);
}

async function loadState() {
  try {
    renderState(await fetchJSON("/api/state"));
  } catch (e) {
    $("updated").textContent = "connection lost (" + hhmmss(Date.now() / 1000) + ")";
  }
}

/* ------------------------------------------------------------------ */
function renderHistory(d) {
  lastGoodHistory = Date.now();
  const ctxWin = STATE && STATE.context_window;

  plots.toks.setData(d.ts, d.tok_s, d.stage);

  const nproc = STATE ? STATE.nproc : null;
  plots.cpu.o.ymax = nproc || null;
  plots.cpu.setData(d.ts, d.cpu, d.stage);

  if (ctxWin) {
    plots.ctx.o.unit = "%";
    plots.ctx.o.fmt = (v) => fmtInt(v);
    plots.ctx.o.tipFmt = (v) => fmt1(v);
    plots.ctx.setData(d.ts, d.context.map((v) => (v == null ? null : 100 * v / ctxWin)), d.stage);
  } else {
    plots.ctx.o.unit = "tok";
    plots.ctx.o.tipFmt = (v) => fmtInt(v);
    plots.ctx.setData(d.ts, d.context, d.stage);
  }

  plots.gpu.setData(d.ts, d.gpu_util, d.stage);

  const vramTotal = d.gpu_vram_total.find((v) => v != null);
  plots.vram.o.ymax = vramTotal ? vramTotal / 1024 : null;
  plots.vram.setData(d.ts, d.gpu_vram_used.map((v) => (v == null ? null : v / 1024)), d.stage);

  plots.ram.setData(d.ts, d.sys_ram_pct, d.stage);
}

async function loadHistory() {
  try {
    renderHistory(await fetchJSON(
      `/api/history?window=${win}&now=${Date.now() / 1000}`));
  } catch (e) {
    /* keep previous data; state poller reports connectivity */
  }
}

/* ------------------------------------------------------------------ */
function initControls() {
  $("ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    win = Number(b.dataset.window);
    for (const x of $("ranges").children) x.classList.toggle("on", x === b);
    loadHistory();
  });
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => Object.values(plots).forEach((p) => p.resize()), 150);
  });
}

initPlots();
initControls();
loadState();
loadHistory();
setInterval(loadState, 1000);
setInterval(loadHistory, 5000);
setTimeout(() => Object.values(plots).forEach((p) => p.resize()), 250);
