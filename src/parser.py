"""Parse journal/syslog lines produced by the strata LLM engine into typed events."""

import re
import time
from datetime import datetime

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

ISO_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<tz>[+-]\d{2}:?\d{2}|Z)?\s+(?P<rest>.*)$")
SHORT_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<rest>.*)$")
HOST_RE = re.compile(
    r"^(?P<host>\S+)\s+(?P<prog>[^\[\s]+)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$")

PROMPT_RE = re.compile(
    r"^reading the prompt:\s+(?P<read>[\d,]+)(?:\s+of\s+(?P<total>[\d,]+))?"
    r"\s+tokens(?:,\s+(?P<sec>\d+)\s+s so far)?")
GEN_RE = re.compile(
    r"^(?P<stage>thinking|answering):\s+(?P<n>[\d,]+) of"
    r"(?: budget (?P<budget>[\d,]+) \(max (?P<hard_max>[\d,]+)\)"
    r"| max (?P<max>[\d,]+))"
    r" tokens, (?P<toks>[\d.]+) tok/s, (?P<sec>\d+) s")
TOOL_RE = re.compile(
    r"^writing a tool call:\s+(?P<tool>\S+):\s+(?P<n>[\d,]+) of max (?P<max>[\d,]+) tokens,"
    r" (?P<toks>[\d.]+) tok/s, (?P<sec>\d+) s")
TOOL_DONE_RE = re.compile(
    r"^tool call complete:\s+(?P<n>[\d,]+) of max (?P<max>[\d,]+) tokens,"
    r" (?P<toks>[\d.]+) tok/s, (?P<sec>\d+) s")
DONE_RE = re.compile(
    r"^done:\s+(?P<n>[\d,]+) tokens in (?P<sec>\d+) s"
    r" \((?:(?P<toks>[\d.]+) tok/s|no speed)\)"
    r" \((?P<reason>[^)]*)\)(?:, expert cache (?P<cache>[\d.]+)% hit"
    r" \((?P<pcie>[+-]?[\d.]+)% of the routed experts over PCIe\))?")
READY_RE = re.compile(r"context (?P<ctx>[\d,]+) tokens")
EXIT_RE = re.compile(r"exit code (?P<code>-\d+|\d+|None)")
PROG_MODEL_RE = re.compile(r"^run-(?P<model>.+?)\.sh$")

NUM = re.compile(r"[\d,]+")

_UNIT_CACHE = {}


def _unit_matcher(unit):
    """(regex, is_template, base, suffix) for the configured unit.

    A template unit ('strata@.service') matches every instance
    ('strata@<model>.service'); the instance name is the model.
    """
    m = _UNIT_CACHE.get(unit)
    if m is None:
        if "@." in unit:
            base, _, suffix = unit.partition("@.")
            pat = re.compile(re.escape(base) + r"@(?P<inst>[A-Za-z0-9_.\-]+)\."
                             + re.escape(suffix))
            m = (pat, True, base, suffix)
        else:
            m = (re.compile(re.escape(unit)), False, None, None)
        _UNIT_CACHE[unit] = m
    return m


def _num(s):
    return int(s.replace(",", ""))


def _epoch_short(mon, day, time_str, now_ts):
    """Journal 'short' format has no year; pick the year that keeps ts near now."""
    month = MONTHS[mon]
    hh, mm, ss = (int(x) for x in time_str.split(":"))
    for year in (datetime.now().year, datetime.now().year - 1):
        try:
            dt = datetime(year, month, int(day), hh, mm, ss)
        except ValueError:
            continue
        ts = time.mktime(dt.timetuple())
        if abs(ts - now_ts) < 300 * 86400:
            return int(ts)
    return int(now_ts)


def parse_timestamp(line, now_ts=None):
    """Return epoch seconds for a journal line, or None if it has no timestamp."""
    now_ts = now_ts if now_ts is not None else time.time()
    m = ISO_RE.match(line)
    if m:
        tzs = m.group("tz")
        naive = datetime.fromisoformat(m.group("date") + "T" + m.group("time"))
        if tzs in (None, "Z"):
            return int(time.mktime(naive.timetuple()))
        if tzs and ":" not in tzs:
            tzs = tzs[:3] + ":" + tzs[3:]
        return int(naive.timestamp())
    m = SHORT_RE.match(line)
    if m:
        return _epoch_short(m.group("mon"), m.group("day"), m.group("time"), now_ts)
    return None


def _msg(line, now_ts=None):
    """Split a journal line into (ts, host, prog, pid, msg)."""
    ts = parse_timestamp(line, now_ts)
    rest = ISO_RE.match(line)
    if rest:
        rest = rest.group("rest")
    else:
        rest = SHORT_RE.match(line)
        if not rest:
            return None
        rest = rest.group("rest")
    m = HOST_RE.match(rest)
    if not m:
        return None
    return ts, m.group("host"), m.group("prog"), m.group("pid"), m.group("msg")


STARTING_KEYWORDS = (
    "starting the engine",
    "loading the model",
    "loading the experts",
    "filling the GPU's expert cache",
    "almost ready",
    "loading the vision encoder",
)


def _ready(base, msg):
    m = READY_RE.search(msg)
    u = re.search(r"(http://\S+)", msg)
    url = u.group(1) if u else None
    if url and url.endswith("/v1"):
        url = url[:-3]
    return dict(base, kind="ready", detail=msg,
                context_window=_num(m.group("ctx")) if m else None,
                server_url=url)


def parse_line(line, now_ts=None, unit="strata.service"):
    """Turn one journal line into an event dict, or None if it is not interesting."""
    parsed = _msg(line, now_ts)
    if parsed is None:
        return None
    ts, host, prog, pid, msg = parsed
    if ts is None:
        return None
    base = {"ts": ts, "host": host, "pid": int(pid) if pid else None}
    matcher, is_tpl, u_base, u_suffix = _unit_matcher(unit)

    if prog == "systemd":
        mu = matcher.search(msg)
        if not mu:
            return None
        if is_tpl:
            base["unit"] = mu.group(0)
            base["model"] = mu.group("inst")
        if msg.startswith("Started "):
            return dict(base, kind="systemd", action="started")
        if msg.startswith("Main process exited"):
            m = re.search(r"status=(\S+)", msg)
            return dict(base, kind="systemd", action="exited",
                        status=m.group(1) if m else None)
        if "Failed with result" in msg:
            return dict(base, kind="systemd", action="failed")
        if msg.startswith("Stopping "):
            return dict(base, kind="systemd", action="stopping")
        if msg.startswith("Stopped ") or "Deactivated successfully" in msg:
            return dict(base, kind="systemd", action="stopped")
        if "Scheduled restart job" in msg:
            return dict(base, kind="systemd", action="restarting")
        return None

    pm = PROG_MODEL_RE.match(prog or "")
    if pm:
        base["model"] = pm.group("model")
        if is_tpl:
            base["unit"] = "%s@%s.%s" % (u_base, pm.group("model"), u_suffix)

    msg_clean = msg.lstrip()
    if msg_clean.startswith("[strata] "):
        msg = msg_clean[len("[strata] "):].rstrip()
    elif msg_clean.startswith("[strata]"):
        msg = msg_clean[len("[strata]"):].lstrip().rstrip()
    else:
        msg_l = msg_clean
        if msg_l.startswith("ready:"):
            return _ready(base, msg_l)
        m = re.match(r"Strata - (\S+) on ", msg_l)
        if m:
            return dict(base, kind="meta", model=m.group(1))
        m = re.match(r"Starting ([^:]+):", msg_l)
        if m:
            return dict(base, kind="meta", model=m.group(1).strip())
        if any(k in msg or k in msg_l for k in STARTING_KEYWORDS):
            return dict(base, kind="lifecycle", stage="starting", detail=msg)
        return None

    m = PROMPT_RE.match(msg)
    if m:
        read = _num(m.group("read"))
        # a whole prompt that was read at once logs no "of N": read == total
        return dict(base, kind="prefill", read=read,
                    total=_num(m.group("total")) if m.group("total") else read,
                    sec=int(m.group("sec")) if m.group("sec") else None)

    m = GEN_RE.match(msg)
    if m:
        return dict(base, kind="gen", stage=m.group("stage"),
                    n=_num(m.group("n")),
                    max=_num(m.group("max") or m.group("hard_max")),
                    budget=_num(m.group("budget")) if m.group("budget") else None,
                    tok_s=float(m.group("toks")), sec=int(m.group("sec")))

    m = TOOL_RE.match(msg)
    if m:
        return dict(base, kind="gen", stage="tool_call", tool=m.group("tool"),
                    n=_num(m.group("n")), max=_num(m.group("max")),
                    tok_s=float(m.group("toks")), sec=int(m.group("sec")))

    m = TOOL_DONE_RE.match(msg)
    if m:
        return dict(base, kind="tool_complete", n=_num(m.group("n")),
                    max=_num(m.group("max")), tok_s=float(m.group("toks")),
                    sec=int(m.group("sec")))

    if msg.startswith("done:"):
        m = DONE_RE.match(msg)
        if not m:
            return dict(base, kind="done")
        reason = m.group("reason").split(",")[0].strip() or "unknown"
        return dict(base, kind="done", n=_num(m.group("n")),
                    sec=int(m.group("sec")),
                    tok_s=float(m.group("toks")) if m.group("toks") else None,
                    reason=reason,
                    cache=float(m.group("cache")) if m.group("cache") else None,
                    pcie=float(m.group("pcie")) if m.group("pcie") else None)

    if msg.startswith("ready:"):
        return _ready(base, msg)

    m = re.match(r"Strata - (\S+) on ", msg)
    if m:
        return dict(base, kind="meta", model=m.group(1))

    if "engine stopped unexpectedly" in msg or "engine had stopped" in msg:
        m = EXIT_RE.search(msg)
        return dict(base, kind="lifecycle", stage="error",
                    detail=msg, code=m.group("code") if m else None)

    if msg.startswith("stopping") or msg == "stopped":
        return dict(base, kind="lifecycle", stage="stopped", detail=msg)

    if any(k in msg for k in STARTING_KEYWORDS):
        return dict(base, kind="lifecycle", stage="starting", detail=msg)

    return dict(base, kind="note", detail=msg)
