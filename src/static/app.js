/* Strata monitor dashboard (hand-rolled canvas plots, no external assets) */
"use strict";

const $ = (id) => document.getElementById(id);

const pal = (name, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

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
      color: "#4fd1c5", color2: null, ymin: null, ymax: null, bands: true,
      fmt: (v) => fmt1(v), tipFmt: (v) => fmt1(v), unit: "",
    }, opts);
    this.data = { ts: [], v: [], v2: [], stages: [] };
    this.domain = null;
    this.gapThreshold = Infinity;
    this.hover = -1;
    this.pad = { l: 50, r: 12, t: 10, b: 22 };

    this.canvas.addEventListener("mousemove", (e) => this._onMove(e));
    this.canvas.addEventListener("mouseleave", () => {
      this.hover = -1; this.tip.style.display = "none"; this.draw();
    });
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(this.root);
    this.resize();
  }

  destroy() { this._ro.disconnect(); this.root.remove(); }

  setData(ts, v, stages, domain, v2) {
    this.data = { ts: ts || [], v: v || [], v2: v2 || [], stages: stages || [] };
    this.domain = (domain && domain.length === 2 && domain[1] > domain[0])
      ? domain : null;
    this._gapThreshold();
    this.hover = -1;
    this.tip.style.display = "none";
    this.draw();
  }

  _gapThreshold() {
    const ts = this.data.ts;
    if (!ts || ts.length < 2) { this.gapThreshold = Infinity; return; }
    const d = [];
    for (let i = 1; i < ts.length; i++) { const g = ts[i] - ts[i - 1]; if (g > 0) d.push(g); }
    if (!d.length) { this.gapThreshold = Infinity; return; }
    d.sort((a, b) => a - b);
    this.gapThreshold = d[d.length >> 1] * 3;
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
    const vals = this._series().flatMap(([v]) =>
      v.filter((x) => x !== null && x !== undefined && isFinite(x)));
    if (!vals.length) return null;
    const dlo = Math.min(...vals);
    let dhi = Math.max(...vals);
    // robust axis: if the top value is a clear outlier, scale to the bulk of
    // the data so a single bogus reading cannot squash the whole plot
    if (hi === null || hi === undefined) {
      const srt = vals.slice().sort((a, b) => a - b);
      const p95 = srt[Math.max(0, Math.ceil(srt.length * 0.95) - 1)];
      if (p95 > 0 && dhi > p95 * 3) dhi = p95 * 1.15;
    }
    if (lo === null || lo === undefined) lo = Math.min(dlo, 0);
    if (hi === null || hi === undefined) hi = dhi + (dhi - lo) * 0.12 || 1;
    if (hi <= lo) hi = lo + 1;
    return niceScale(lo, hi);
  }

  _geom() {
    const { l, r, t, b } = this.pad;
    return { x0: l, x1: this.w - r, y0: t, y1: this.h - b };
  }

  _tspan() {
    if (this.domain) return [this.domain[0], this.domain[1]];
    const ts = this.data.ts;
    return [ts[0], ts[ts.length - 1] || ts[0] + 1];
  }

  _onMove(e) {
    const ts = this.data.ts;
    if (!ts.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const g = this._geom();
    const [t0, t1] = this._tspan();
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

  _series() {
    const s = [[this.data.v, this.o.color, ""]];
    if (this.o.color2) s.push([this.data.v2, this.o.color2, "<br>"]);
    return s.filter(([v]) => v && v.length);
  }

  _showTip(i, g) {
    const st = this.data.stages[i];
    const meta = STAGES[st];
    const row = (v, fmt, sep) =>
      sep + (v === null || v === undefined ? "–"
        : fmt(v) + (this.o.unit ? " " + this.o.unit : ""));
    this.tip.innerHTML =
      `<span class="t">${hhmmss(this.data.ts[i])}</span><br>` +
      this._series().map(([v, c, sep], k) =>
        (k === 0 ? row(v[i], this.o.tipFmt, sep)
                 : `<span style="color:${c}">${row(v[i], this.o.tipFmt, sep)}</span>`)).join("") +
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
    const [t0, t1] = this._tspan();
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
      ctx.fillStyle = pal("--dim", "#7f8ea8");
      ctx.font = "12px ui-monospace, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.fillText("no data yet", this.w / 2, this.h / 2);
      ctx.textAlign = "left";
      return;
    }
    const scale = this._range();
    if (!scale) return;
    const yOf = (v) => g.y1 - (v - scale.min) / (scale.max - scale.min) * (g.y1 - g.y0);
    const [t0, t1] = this._tspan();
    const xOf = (t) => t1 <= t0 ? (g.x0 + g.x1) / 2
      : g.x0 + (t - t0) / (t1 - t0) * (g.x1 - g.x0);

    // stage bands
    if (this.o.bands && this.data.stages.length) {
      const st = this.data.stages;
      let i = 0;
      while (i < st.length) {
        let j = i;
        while (j + 1 < st.length && st[j + 1] === st[i] &&
               ts[j + 1] - ts[j] <= this.gapThreshold) j++;
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
      ctx.strokeStyle = pal("--grid", "#1a2438");
      ctx.beginPath(); ctx.moveTo(g.x0, y); ctx.lineTo(g.x1, y); ctx.stroke();
      ctx.fillStyle = pal("--dim", "#7f8ea8");
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
      ctx.fillStyle = pal("--dim", "#7f8ea8");
      ctx.textAlign = k === 0 ? "left" : k === ticks ? "right" : "center";
      ctx.fillText(xLabel(t, span), x, this.h - 6);
    }
    ctx.textAlign = "left";

    // lines
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    for (const [series, color] of this._series()) {
      ctx.strokeStyle = color;
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < ts.length; i++) {
        const v = series[i];
        if (v === null || v === undefined || !isFinite(v)) { pen = false; continue; }
        if (pen && i > 0 && ts[i] - ts[i - 1] > this.gapThreshold) pen = false;
        const x = xOf(ts[i]), y = yOf(v);
        if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
      }
      ctx.stroke();
    }

    // hover
    if (this.hover >= 0 && this.hover < ts.length) {
      const x = xOf(ts[this.hover]);
      ctx.strokeStyle = pal("--hover-ring", "#33415588");
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, g.y0); ctx.lineTo(x, g.y1); ctx.stroke();
      ctx.setLineDash([]);
      for (const [series, color] of this._series()) {
        const hv = series[this.hover];
        if (hv === null || hv === undefined || !isFinite(hv)) continue;
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(x, yOf(hv), 3.2, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = pal("--dotstroke", "#0b1220");
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, yOf(hv), 3.2, 0, Math.PI * 2); ctx.stroke();
      }
    }
  }
}

/* ------------------------------------------------------------------ */
const plots = {};
let win = 600;
let STATE = null;
let lastGoodHistory = 0;

const HIST_MS = { 60: 2000, 600: 1000, 7200: 5000, 86400: 15000, 604800: 60000 };
let histTimer = null;

const THEME_KEY = "strata-theme";
function initTheme() {
  const btn = $("theme");
  const saved = localStorage.getItem(THEME_KEY);
  const sysDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  const apply = (t) => {
    document.documentElement.dataset.theme = t;
    btn.textContent = t === "day" ? "☀" : "☾";
    btn.setAttribute("aria-pressed", t === "day" ? "true" : "false");
  };
  apply(saved || (sysDark() ? "night" : "day"));
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "day" ? "night" : "day";
    localStorage.setItem(THEME_KEY, next);
    apply(next);
    Object.values(plots).forEach((p) => p.draw());
  });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (!localStorage.getItem(THEME_KEY))
      apply(sysDark() ? "night" : "day");
  });
}

function scheduleHistory() {
  if (histTimer) clearInterval(histTimer);
  histTimer = setInterval(loadHistory, HIST_MS[win] || 15000);
}

const BANDS_KEY = "strata-bands";
let showBands = true;
function initBands() {
  const btn = $("bands");
  showBands = localStorage.getItem(BANDS_KEY) !== "off";
  const draw = () => {
    btn.classList.toggle("on", showBands);
    btn.setAttribute("aria-pressed", String(showBands));
    Object.values(plots).forEach((p) => { p.o.bands = showBands; p.draw(); });
  };
  draw();
  btn.addEventListener("click", () => {
    showBands = !showBands;
    localStorage.setItem(BANDS_KEY, showBands ? "on" : "off");
    draw();
  });
}

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
  plots.req = new Plot("p-req", {
    color: "#34d399", color2: "#fbbf24", ymin: 0,
    fmt: (v) => fmtInt(v), tipFmt: (v) => fmtInt(v),
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

/* ------------------------------------------------------------------ */
/* GPUs: one card and one utilisation + one memory plot per card       */

const GPU_COLORS = ["#34d399", "#fbbf24", "#60a5fa", "#f472b6",
                    "#a78bfa", "#fb7185", "#4fd1c5", "#94a3b8"];
const gpuColor = (i) => GPU_COLORS[Math.abs(Number(i) || 0) % GPU_COLORS.length];
const shortGpuName = (n) => (n || "").replace(/^(NVIDIA|AMD|Intel)\s+/i, "")
  .replace(/^(GeForce|Radeon)\s+/i, "").trim();

const gpuCards = {};   // gpu index -> card element
const gpuPlots = {};   // gpu index -> {util, vram} Plot instances
const EMPTY_GPU = "-"; // placeholder card's pseudo index (Number("-") is NaN)

/* live per-card readings; the scalar gpu_* fields cover single-card
   setups and monitors that predate the gpus list */
function gpuEntries(s) {
  let g = s.gpus || [];
  if (!g.length && s.gpu_util !== null && s.gpu_util !== undefined) {
    g = [{ index: 0, name: s.gpu_name, util: s.gpu_util,
           vram_used: s.gpu_vram_used, vram_total: s.gpu_vram_total,
           temp: s.gpu_temp }];
  }
  return g.slice().sort((a, b) => (a.index || 0) - (b.index || 0));
}

function dropGone(keep, bag, kill) {
  const alive = new Set(keep.map(Number));
  for (const k of Object.keys(bag)) {
    if (!alive.has(Number(k))) { kill(bag[k], k); delete bag[k]; }
  }
}

function ensureGpuCard(idx) {
  let c = gpuCards[idx];
  if (!c) {
    c = document.createElement("div");
    c.className = "card gpu-card";
    c.innerHTML = `<div class="label"></div>` +
      `<div class="big"><span class="gu">–</span><span class="unit">util %</span></div>` +
      `<div class="sub gs">–</div>`;
    gpuCards[idx] = c;
  }
  $("card-lr").parentNode.insertBefore(c, $("card-lr")); // keeps card order
  return c;
}

function renderGpuCards(entries) {
  dropGone(entries.map((g) => g.index), gpuCards, (c) => c.remove());
  if (!entries.length) {
    const c = ensureGpuCard(EMPTY_GPU);
    c.querySelector(".label").textContent = "GPU";
    c.querySelector(".gu").textContent = "–";
    c.querySelector(".gs").textContent = "no GPU readings";
    c.style.borderLeft = "3px solid " + gpuColor(0);
    return;
  }
  for (const g of entries) {
    const c = ensureGpuCard(g.index);
    const nm = shortGpuName(g.name);
    c.querySelector(".label").textContent =
      nm ? `GPU ${g.index} · ${nm}` : `GPU ${g.index}`;
    c.querySelector(".gu").textContent = fmtInt(g.util);
    let sub = `VRAM ${fmt1((g.vram_used || 0) / 1024)} / ` +
      `${fmt1((g.vram_total || 0) / 1024)} GiB`;
    if (g.temp !== null && g.temp !== undefined) sub += ` · ${fmtInt(g.temp)} °C`;
    c.querySelector(".gs").textContent = sub;
    c.style.borderLeft = `3px solid ${gpuColor(g.index)}`;
  }
}

function ensureGpuPlots(entries) {
  const anchor = $("p-ram");
  dropGone(entries.map((g) => g.index), gpuPlots, (p, k) => {
    delete plots["gpu" + k + "util"];
    delete plots["gpu" + k + "vram"];
    p.util.destroy();
    p.vram.destroy();
  });
  for (const g of entries) {
    let p = gpuPlots[g.index];
    if (!p) {
      p = {};
      const defs = [
        ["util", "utilization", "%", { unit: "%", ymin: 0, ymax: 100,
          fmt: (v) => fmtInt(v), tipFmt: (v) => fmtInt(v) }],
        ["vram", "memory", "GiB", { unit: "GiB", ymin: 0,
          fmt: (v) => fmt1(v), tipFmt: (v) => fmt2(v) }],
      ];
      for (const [key, title, hint, opts] of defs) {
        const div = document.createElement("div");
        div.className = "plot";
        div.id = `p-${key}-gpu${g.index}`;
        div.innerHTML = `<h3><i style="background:${gpuColor(g.index)}"></i>` +
          `GPU ${g.index} ${title} <span class="hint">${hint}</span></h3>` +
          `<canvas></canvas><div class="tip"></div>`;
        anchor.parentNode.insertBefore(div, anchor);
        p[key] = new Plot(div.id, Object.assign({ color: gpuColor(g.index) }, opts));
        plots["gpu" + g.index + key] = p[key];
      }
      gpuPlots[g.index] = p;
    }
    // keep plot order stable when a new card joins at any position
    anchor.parentNode.insertBefore(p.util.root, anchor);
    anchor.parentNode.insertBefore(p.vram.root, anchor);
  }
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
  let sub;
  if (s.stage === "prefill" && s.prompt) {
    sub = `reading ${fmtInt(s.context)} of ${fmtInt(s.prompt)} prompt tokens`;
  } else {
    sub = `for ${dur(s.now - s.stage_since)}`;
  }
  if (s.tool) sub += ` · tool: ${s.tool}`;
  if (s.stale) sub += ` · no log for ${dur(s.age_s)}`;
  else if (s.age_s !== null && s.age_s > 2 && !ACTIVE.has(s.stage))
    sub += ` · last log ${dur(s.age_s)} ago`;
  $("stage-sub").textContent = sub;

  // speed
  $("toks").textContent = fmt1(s.tok_s);
  let sp = "idle";
  if (s.stage === "prefill") {
    sp = `reading prompt · ${dur(s.elapsed)}`;
  } else if (ACTIVE.has(s.stage)) {
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
  if (s.stage === "prefill") {
    cs = "prefill";
  } else if (s.context != null) {
    cs = `prompt ${fmtInt(s.prompt)} · generated ${fmtInt(s.gen)}`;
    cs += ACTIVE.has(s.stage) ? " · request running" : " · last request";
  }
  $("ctx-sub").textContent = cs;

  // gpus: one card and one pair of plots per card
  const gents = gpuEntries(s);
  renderGpuCards(gents);
  ensureGpuPlots(gents);

  // parallel requests + batch slots (the engine's "parallel": N mode)
  const par = s.parallel;
  $("card-req").hidden = !par;
  $("p-req").hidden = !par;
  if (par) {
    $("req-par").textContent = "· parallel " + par;
    const run = s.req_running || 0;
    const busy = s.slots_busy || 0;
    $("req-run").textContent = fmtInt(run);
    $("req-cap").textContent = s.slots_total ? `of ${s.slots_total} slots` : "running";
    const bits = [];
    if (s.req_waiting) bits.push(`waiting ${s.req_waiting}`);
    if (s.req_queued) bits.push(`queued ${s.req_queued}`);
    if (run > busy) bits.push(`${run - busy} on the solo path (outside slots)`);
    $("req-sub").textContent =
      bits.length ? bits.join(" · ") : (run ? "engine busy" : "all slots idle");
    $("slots").innerHTML = (s.slots || []).map((sl) => {
      const st = sl.state || "idle";
      const b = [`#${sl.slot == null ? "?" : sl.slot} ${st}`];
      if (st === "reading" && sl.prompt_read != null)
        b.push(`${fmtInt(sl.prompt_read)}${sl.prompt_tokens ? "/" + fmtInt(sl.prompt_tokens) : ""} tok`);
      if (sl.generated) b.push(`${fmtInt(sl.generated)} tok`);
      if (sl.tok_s) b.push(`${fmt1(sl.tok_s)} t/s`);
      return `<span class="chip ${esc(st)}">${b.join(" · ")}</span>`;
    }).join("");
  } else {
    $("req-par").textContent = "";
    $("slots").innerHTML = "";
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

  $("src").textContent = `source ${s.source}` +
    (s.active_unit ? ` · unit ${s.active_unit}` : "") +
    ` · ${fmtInt(s.samples)} samples · retention ${s.history_retention_days} d`;
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
  const dom = (d.from_ts != null && d.now != null) ? [d.from_ts, d.now] : null;

  plots.toks.setData(d.ts, d.tok_s, d.stage, dom);

  const nproc = STATE ? STATE.nproc : null;
  plots.cpu.o.ymax = nproc || null;
  plots.cpu.setData(d.ts, d.cpu, d.stage, dom);

  if (ctxWin) {
    plots.ctx.o.unit = "%";
    plots.ctx.o.fmt = (v) => fmtInt(v);
    plots.ctx.o.tipFmt = (v) => fmt1(v);
    plots.ctx.setData(d.ts, d.context.map((v) => (v == null ? null : 100 * v / ctxWin)), d.stage, dom);
  } else {
    plots.ctx.o.unit = "tok";
    plots.ctx.o.tipFmt = (v) => fmtInt(v);
    plots.ctx.setData(d.ts, d.context, d.stage, dom);
  }

  // per-card GPU history; fall back to the aggregate scalars when the
  // gpu_samples table is still empty (older data or a pre-multi-GPU build)
  let ghist = d.gpus || [];
  if (!ghist.length && (d.gpu_util || []).some((v) => v != null)) {
    ghist = [{ index: 0, ts: d.ts, util: d.gpu_util,
               vram_used: d.gpu_vram_used, vram_total: d.gpu_vram_total }];
  }
  for (const p of Object.values(gpuPlots)) {
    p.util.setData(null, null, null, dom);
    p.vram.setData(null, null, null, dom);
    p.vram.o.ymax = null;
  }
  for (const g of ghist) {
    const p = gpuPlots[g.index];
    if (!p) continue;
    p.util.setData(g.ts, g.util, null, dom);
    const cap = (g.vram_total || []).find((v) => v != null);
    p.vram.o.ymax = cap ? cap / 1024 : null;
    p.vram.setData(g.ts, (g.vram_used || []).map((v) => (v == null ? null : v / 1024)),
      null, dom);
  }

  // requests: running vs waiting (parallel mode only)
  plots.req.setData(d.ts, d.req_running, d.stage, dom, d.req_waiting);

  plots.ram.setData(d.ts, d.sys_ram_pct, d.stage, dom);
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
    scheduleHistory();
  });
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => Object.values(plots).forEach((p) => p.resize()), 150);
  });
}

initTheme();
initPlots();
initControls();
initBands();
loadState();
loadHistory();
setInterval(loadState, 1000);
scheduleHistory();
setTimeout(() => Object.values(plots).forEach((p) => p.resize()), 250);
