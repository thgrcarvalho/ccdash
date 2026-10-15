#!/usr/bin/env python3
"""ccdash: a local dashboard of where Claude Code spends your tokens.

Reads the JSONL transcripts Claude Code keeps under ~/.claude/projects (read-only),
aggregates them, and writes one self-contained HTML file. Python 3.9+ standard
library only; no network, nothing leaves the machine.

    python3 ccdash.py                       # last 30 days -> ./ccdash.html
    python3 ccdash.py --since 7 --open      # last 7 days, open in the browser
    python3 ccdash.py --config config.json  # pricing, aliases, cache TTL for this machine
    python3 ccdash.py --json data.json      # dump the aggregated data instead
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import glob
import json
import math
import os
import sys
import webbrowser

# ---------------------------------------------------------------- pricing ----
# USD per million tokens, Anthropic list prices (2026-10). cache_write_5m = 1.25 x input,
# cache_write_1h = 2 x input unless given. Keys are matched as substrings of the model id,
# in this order, after aliases from the config are applied.
DEFAULT_PRICES = {
    "fable-5-1":  {"input": 10,   "output": 50,  "cache_read": 0.25},
    "fable":      {"input": 10,   "output": 50,  "cache_read": 1.00},
    "opus-5-5":   {"input": 4,    "output": 20,  "cache_read": 0.20},
    "opus-5":     {"input": 5,    "output": 25,  "cache_read": 0.50},
    "opus-4":     {"input": 5,    "output": 25,  "cache_read": 0.50},
    "sonnet-5-5": {"input": 2,    "output": 10,  "cache_read": 0.10},
    "sonnet-5":   {"input": 2,    "output": 10,  "cache_read": 0.20},
    "sonnet-4":   {"input": 3,    "output": 15,  "cache_read": 0.30},
    "haiku-5":    {"input": 0.10, "output": 0.50, "cache_read": 0.01},
    "haiku-4":    {"input": 1,    "output": 5,   "cache_read": 0.10},
}
DEFAULT_CONFIG = {
    "ttl_minutes": "auto",      # "auto" reads the lifetime from the usage fields; or 60 (subscription) / 5 (API, Bedrock, gateway)
    "price_multiplier": 1.0,    # e.g. 1.10 for Bedrock regional (us./eu.) inference profiles
    "chars_per_token": 3.5,     # for tool-result estimates only; real usage comes from the API fields
    "aliases": {},              # {"gateway-model-name": "claude-sonnet-5-5"}
    "prices": {},               # same shape as DEFAULT_PRICES; merged on top
    "top_sessions": 15,
    "top_tools": 30,
}


class Pricing:
    def __init__(self, cfg):
        self.table = dict(DEFAULT_PRICES)
        self.table.update(cfg.get("prices", {}))
        self.aliases = cfg.get("aliases", {})
        self.mult = float(cfg.get("price_multiplier", 1.0))
        self.unknown = set()

    def resolve(self, model):
        m = self.aliases.get(model, model) or ""
        low = m.lower()
        for key in self.table:
            if key.lower() in low:
                return self.table[key], True
        self.unknown.add(model)
        return self.table["sonnet-5"], False

    def cost(self, model, inp=0, out=0, cw5=0, cw1=0, cr=0):
        p, _ = self.resolve(model)
        w5 = p.get("cache_write_5m", p["input"] * 1.25)
        w1 = p.get("cache_write_1h", p["input"] * 2.0)
        return self.mult * (inp * p["input"] + out * p["output"] + cw5 * w5 + cw1 * w1 + cr * p["cache_read"]) / 1e6

    def parts(self, r):
        """Cost split of one request record into input / cache_write / cache_read / output."""
        return {
            "input": self.cost(r["model"], inp=r["in"]),
            "cache_write": self.cost(r["model"], cw5=r["cw"] - r["cw1"], cw1=r["cw1"]),
            "cache_read": self.cost(r["model"], cr=r["cr"]),
            "output": self.cost(r["model"], out=r["out"]),
        }


# ---------------------------------------------------------------- parsing ----
def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return 0


def image_tokens(dim):
    try:
        return math.ceil(dim["width"] / 28) * math.ceil(dim["height"] / 28)
    except Exception:
        return 1500


def result_tokens(block, tur, cpt):
    """Estimated tokens one tool_result block added to the context."""
    n = text_len(block.get("content"))
    imgs = 0
    if isinstance(block.get("content"), list):
        for b in block["content"]:
            if isinstance(b, dict) and b.get("type") == "image":
                f = tur.get("file", {}) if isinstance(tur, dict) else {}
                imgs += image_tokens(f.get("dimensions") or {})
    if isinstance(tur, dict):
        f = tur.get("file")
        if isinstance(f, dict) and isinstance(f.get("content"), str):
            n = max(n, len(f["content"]))  # the Read tool stores the text here
        elif n == 0 and tur.get("stdout") is not None:
            n = len(tur.get("stdout") or "") + len(tur.get("stderr") or "")
    return n / cpt + imgs


def find_files(root):
    main = glob.glob(os.path.join(root, "*", "*.jsonl"))
    subs = glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    return main, subs


def read_file(path, root, cpt):
    """One pass over a transcript. Returns a list of ordered events and session metadata.

    Event kinds: ('req', record) for one API response (deduplicated by message id),
                 ('res', tool_name, est_tokens, ts) for one tool_result block.
    """
    rel = os.path.relpath(path, root).split(os.sep)
    project = rel[0]
    is_sub = "subagents" in rel
    sid = rel[1] if is_sub else rel[1][:-6]
    events, by_id, toolnames = [], {}, {}
    meta = {"turns": 0, "images": 0, "first": None, "last": None}
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            m, t, when = d.get("message"), d.get("type"), parse_ts(d.get("timestamp", ""))
            if when:
                if not meta["first"] or when < meta["first"]:
                    meta["first"] = when
                if not meta["last"] or when > meta["last"]:
                    meta["last"] = when
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if t == "assistant":
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            toolnames[b.get("id")] = b.get("name")
                u = m.get("usage") or {}
                if not u or m.get("model") in (None, "<synthetic>"):
                    continue
                mid = m.get("id") or d.get("uuid")
                cc = u.get("cache_creation") or {}
                cw = u.get("cache_creation_input_tokens") or 0
                rec = {
                    "model": m["model"], "in": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                    "cw": cw, "cw1": min(cc.get("ephemeral_1h_input_tokens") or 0, cw), "has_cc": bool(cc),
                    "cr": u.get("cache_read_input_tokens") or 0,
                    "think": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                    "ts": when, "sid": sid, "project": project, "sub": is_sub,
                }
                if mid in by_id:                       # streamed chunks repeat the id; keep the final one
                    old = by_id[mid]
                    if rec["out"] >= old["out"]:
                        old.update({k: rec[k] for k in ("out", "think")})
                    continue
                by_id[mid] = rec
                events.append(("req", rec))
            elif t == "user":
                if isinstance(c, list):
                    tur = d.get("toolUseResult")
                    has_text = False
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_result":
                            events.append(("res", toolnames.get(b.get("tool_use_id"), "?"),
                                           result_tokens(b, tur, cpt), when))
                            if isinstance(b.get("content"), list):
                                meta["images"] += sum(1 for x in b["content"] if isinstance(x, dict) and x.get("type") == "image")
                        elif bt == "image":
                            meta["images"] += 1
                        elif bt == "text":
                            has_text = True
                    if has_text and not is_sub and not d.get("isMeta"):
                        meta["turns"] += 1
                elif isinstance(c, str) and not is_sub:
                    meta["turns"] += 1
    return dict(project=project, sid=sid, sub=is_sub, events=events, meta=meta, path=path)


# ------------------------------------------------------------ aggregation ----
def downsample(series, n=40):
    if len(series) <= n:
        return series
    step = len(series) / n
    out = [series[int(i * step)] for i in range(n)]
    if out[-1] is not series[-1]:
        out.append(series[-1])
    return out


def detect_ttl(parsed, in_period):
    """Cache lifetime of the main conversation, read from the usage fields.

    Returns (minutes, source, share_of_1h_writes). The API reports each cache write as
    ephemeral_5m or ephemeral_1h; if most main-conversation writes are 1h the lifetime is
    60 minutes, otherwise 5. Without the breakdown (some gateways drop it) assume 5.
    """
    cw = cw1 = 0
    seen_breakdown = False
    for f in parsed:
        if f["sub"]:
            continue
        for e in f["events"]:
            if e[0] == "req" and in_period(e[1]["ts"]):
                cw += e[1]["cw"]
                cw1 += e[1]["cw1"]
                seen_breakdown = seen_breakdown or e[1]["has_cc"]
    if not seen_breakdown or cw == 0:
        return 5, "assumed 5 min: the usage fields carry no cache-lifetime breakdown", None
    share = cw1 / cw
    if share >= 0.5:
        return 60, f"observed: {share*100:.0f}% of main-conversation cache writes used the 1-hour lifetime", share
    return 5, f"observed: {(1-share)*100:.0f}% of main-conversation cache writes used the 5-minute lifetime", share


def aggregate(root, cfg, since_days, pricing):
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=since_days) if since_days else None
    cpt = float(cfg["chars_per_token"])
    main, subs = find_files(root)
    parsed = [read_file(p, root, cpt) for p in main + subs]

    in_period = lambda ts: ts is not None and (cutoff is None or ts >= cutoff)
    if str(cfg["ttl_minutes"]).lower() == "auto":
        ttl_min, ttl_source, share_1h = detect_ttl(parsed, in_period)
    else:
        ttl_min, ttl_source, share_1h = float(cfg["ttl_minutes"]), "from config", None
    totals = collections.Counter()
    comp = collections.Counter()
    daily = collections.defaultdict(lambda: collections.Counter())
    daily_sessions = collections.defaultdict(set)
    models = collections.defaultdict(collections.Counter)
    projects = collections.defaultdict(collections.Counter)
    project_sessions = collections.defaultdict(set)
    sessions = collections.defaultdict(lambda: dict(cost=0.0, requests=0, tokens=0, cr=0, inside=0, peak=0,
                                                    models=collections.Counter(), growth=[], idle=0, breaker=0,
                                                    subagents=0, images=0, turns=0, first=None, last=None, project=""))
    misses = {k: collections.Counter(n=0, tok=0, cost=0.0) for k in ("first", "idle", "breaker")}
    GAP_BUCKETS = ["5-15 min", "15-60 min", "1-4 h", "4-24 h", "> 24 h"]
    idle_gaps = {b: collections.Counter(n=0, cost=0.0) for b in GAP_BUCKETS}
    whatif = collections.Counter(now=0.0, alt=0.0, extra_rewrites=0)   # main-conversation cache writes, observed vs other lifetime

    def gap_bucket(g):
        return GAP_BUCKETS[0] if g <= 15 else GAP_BUCKETS[1] if g <= 60 else GAP_BUCKETS[2] if g <= 240 else GAP_BUCKETS[3] if g <= 1440 else GAP_BUCKETS[4]
    tools = collections.defaultdict(collections.Counter)
    servers = collections.defaultdict(collections.Counter)
    biggest = []
    hourly = collections.Counter()
    weekday = collections.Counter()
    real_new = est_new = 0.0

    for f in parsed:
        ev = f["events"]
        S = sessions[f["sid"]]
        S["project"] = f["project"]
        if f["sub"]:
            S["subagents"] += 1
        else:
            S["images"] += f["meta"]["images"]
            S["turns"] += f["meta"]["turns"]
        for key in ("first", "last"):
            v = f["meta"][key]
            if v and (S[key] is None or (v < S[key] if key == "first" else v > S[key])):
                S[key] = v

        reqs = [(i, e[1]) for i, e in enumerate(ev) if e[0] == "req"]
        # --- per request ---
        prev = None
        for i, r in reqs:
            ctx = r["in"] + r["cw"] + r["cr"]
            r["ctx"] = ctx
            if not f["sub"]:
                S["growth"].append(ctx)
            if in_period(r["ts"]):
                c = pricing.parts(r)
                cost = sum(c.values())
                r["cost"] = cost
                for k, v in c.items():
                    comp[k] += v
                for k in ("in", "out", "cw", "cw1", "cr", "think"):
                    totals[k] += r[k]
                totals["cost"] += cost
                totals["requests"] += 1
                day = r["ts"].astimezone().strftime("%Y-%m-%d")
                D = daily[day]
                for k in ("in", "out", "cw", "cr"):
                    D[k] += r[k]
                for k, v in c.items():
                    D["c_" + k] += v
                D["cost"] += cost
                D["requests"] += 1
                daily_sessions[day].add(f["sid"])
                M = models[r["model"]]
                for k in ("in", "out", "cw", "cr"):
                    M[k] += r[k]
                M["cost"] += cost
                M["requests"] += 1
                P = projects[f["project"]]
                P["cost"] += cost
                P["tokens"] += ctx + r["out"]
                P["cr"] += r["cr"]
                P["inside"] += ctx
                P["requests"] += 1
                project_sessions[f["project"]].add(f["sid"])
                S["cost"] += cost
                S["requests"] += 1
                S["tokens"] += ctx + r["out"]
                S["cr"] += r["cr"]
                S["inside"] += ctx
                S["peak"] = max(S["peak"], ctx) if not f["sub"] else S["peak"]
                S["models"][r["model"]] += 1
                lt = r["ts"].astimezone()
                hourly[lt.hour] += cost
                weekday[lt.weekday()] += cost
                # cache rewrite classification (large writes only)
                gap_min = None
                if prev is not None and prev["ts"] and r["ts"]:
                    gap_min = (r["ts"] - prev["ts"]).total_seconds() / 60
                cls = None
                wcost = pricing.cost(r["model"], cw5=r["cw"] - r["cw1"], cw1=r["cw1"])
                if r["cw"] > 20000:
                    if prev is None:
                        cls = "first"
                    elif gap_min is not None and gap_min > ttl_min:
                        cls = "idle"
                    elif prev["ctx"] > 0 and r["cw"] > 0.5 * prev["ctx"]:
                        cls = "breaker"
                    if cls:
                        misses[cls]["n"] += 1
                        misses[cls]["tok"] += r["cw"]
                        misses[cls]["cost"] += wcost
                        if cls in ("idle", "breaker") and not f["sub"]:
                            S[cls] += 1
                        if cls == "idle":
                            b = gap_bucket(gap_min)
                            idle_gaps[b]["n"] += 1
                            idle_gaps[b]["cost"] += wcost
                # what-if: the same session under the other cache lifetime (main conversation only)
                if not f["sub"]:
                    whatif["now"] += wcost
                    if ttl_min <= 5:
                        if not (cls == "idle" and gap_min <= 60):          # a 1h cache would still be warm
                            whatif["alt"] += pricing.cost(r["model"], cw1=r["cw"])
                    else:
                        whatif["alt"] += pricing.cost(r["model"], cw5=r["cw"])
                        if gap_min is not None and 5 < gap_min <= ttl_min and cls is None and prev["ctx"] > 20000:
                            whatif["alt"] += pricing.cost(r["model"], cw5=prev["ctx"])   # would have expired
                            whatif["extra_rewrites"] += 1
            prev = r
        # --- per tool result ---
        req_idx = [i for i, _ in reqs]
        ctx_at = {i: r["ctx"] for i, r in reqs}
        bounds = [cur for p_, cur in zip(req_idx, req_idx[1:]) if ctx_at[cur] < ctx_at[p_] * 0.6]
        pending, prev_out = 0.0, 0
        for i, e in enumerate(ev):
            if e[0] == "res":
                pending += e[2]
                if not in_period(e[3]):
                    continue
                k = bisect.bisect_right(req_idx, i)       # position of the next request
                if k >= len(req_idx):
                    continue
                nxt = req_idx[k]
                b = bisect.bisect_right(bounds, nxt)       # first compaction/clear after that request
                end = bisect.bisect_left(req_idx, bounds[b]) if b < len(bounds) else len(req_idx)
                n_later = end - k
                model = ev[nxt][1]["model"]
                carried = e[2] * max(0, n_later - 1)
                cost = pricing.cost(model, cw5=e[2], cr=carried)
                T = tools[e[1]]
                T["calls"] += 1
                T["direct"] += e[2]
                T["carried"] += carried
                T["cost"] += cost
                T["max"] = max(T["max"], e[2])
                if e[1].startswith("mcp__"):
                    V = servers[e[1].split("__")[1]]
                    V["calls"] += 1
                    V["direct"] += e[2]
                    V["carried"] += carried
                    V["cost"] += cost
                biggest.append((round(e[2]), e[1], f["project"], f["sid"][:8],
                                e[3].astimezone().strftime("%Y-%m-%d %H:%M") if e[3] else "?"))
            else:
                r = e[1]
                if in_period(r["ts"]):
                    miss = r["cr"] == 0 and r["cw"] > 30000
                    if not miss and (pending or prev_out):
                        real_new += r["cw"] + r["in"]
                        est_new += pending + prev_out
                pending, prev_out = 0.0, r["out"]

    # --- shape for the template ---
    inside = totals["in"] + totals["cw"] + totals["cr"]
    days_sorted = sorted(daily)
    daily_rows = [dict(day=d, sessions=len(daily_sessions[d]), **{k: round(v, 4) for k, v in daily[d].items()}) for d in days_sorted]
    costs = [daily[d]["cost"] for d in days_sorted]
    med = sorted(costs)[len(costs) // 2] if costs else 0
    model_rows = sorted([dict(model=m, hit=(x["cr"] / (x["in"] + x["cw"] + x["cr"]) if (x["in"] + x["cw"] + x["cr"]) else 0),
                              known=pricing.resolve(m)[1], **{k: round(v, 4) for k, v in x.items()}) for m, x in models.items()],
                        key=lambda r: -r["cost"])
    project_rows = sorted([dict(project=p, sessions=len(project_sessions[p]), hit=(x["cr"] / x["inside"] if x["inside"] else 0),
                                **{k: round(v, 4) for k, v in x.items()}) for p, x in projects.items()], key=lambda r: -r["cost"])
    sess_rows = []
    for sid, S in sessions.items():
        if S["requests"] == 0:
            continue
        sess_rows.append(dict(
            sid=sid, short=sid[:8], project=S["project"], cost=round(S["cost"], 4), requests=S["requests"],
            tokens=S["tokens"], hit=(S["cr"] / S["inside"] if S["inside"] else 0), peak=S["peak"], turns=S["turns"],
            idle=S["idle"], breaker=S["breaker"], subagents=S["subagents"], images=S["images"],
            start=S["first"].astimezone().strftime("%Y-%m-%d %H:%M") if S["first"] else "?",
            hours=round((S["last"] - S["first"]).total_seconds() / 3600, 1) if S["first"] and S["last"] else 0,
            models=[[m.replace("claude-", ""), n] for m, n in S["models"].most_common(4)],
            growth=downsample(S["growth"]),
        ))
    sess_rows.sort(key=lambda r: -r["cost"])
    tool_rows = sorted([dict(tool=t, **{k: round(v, 2) for k, v in x.items()}) for t, x in tools.items()], key=lambda r: -r["cost"])
    server_rows = sorted([dict(server=s, **{k: round(v, 2) for k, v in x.items()}) for s, x in servers.items()], key=lambda r: -r["cost"])

    return {
        "meta": {
            "generated": now.astimezone().strftime("%Y-%m-%d %H:%M"), "since_days": since_days, "root": root,
            "files": len(main) + len(subs), "subagent_files": len(subs),
            "ttl_minutes": int(ttl_min) if float(ttl_min).is_integer() else ttl_min, "ttl_source": ttl_source,
            "share_1h": None if share_1h is None else round(share_1h, 3),
            "price_multiplier": cfg["price_multiplier"], "unknown_models": sorted(pricing.unknown),
            "calibration": round(real_new / est_new, 2) if est_new else None,
        },
        "totals": {**{k: int(v) if k != "cost" else round(v, 2) for k, v in totals.items()},
                   "inside": int(inside), "hit": (totals["cr"] / inside if inside else 0),
                   "sessions": len(sess_rows), "components": {k: round(v, 2) for k, v in comp.items()},
                   "median_day": round(med, 2), "max_day": round(max(costs), 2) if costs else 0,
                   "active_days": len(costs)},
        "daily": daily_rows,
        "models": model_rows,
        "projects": project_rows,
        "sessions": sess_rows[: int(cfg["top_sessions"])],
        "misses": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in misses.items()},
        "idle_gaps": [dict(bucket=b, n=idle_gaps[b]["n"], cost=round(idle_gaps[b]["cost"], 2)) for b in GAP_BUCKETS],
        "whatif": {"ttl_now": ttl_min, "ttl_alt": 60 if ttl_min <= 5 else 5, "write_cost_now": round(whatif["now"], 4),
                   "write_cost_alt": round(whatif["alt"], 4), "extra_rewrites": whatif["extra_rewrites"]},
        "tools": tool_rows[: int(cfg["top_tools"])],
        "tools_total": {k: round(sum(t[k] for t in tool_rows), 2) for k in ("calls", "direct", "carried", "cost")},
        "servers": server_rows,
        "biggest": sorted(biggest, reverse=True)[:12],
        "hourly": [round(hourly[h], 2) for h in range(24)],
        "weekday": [round(weekday[d], 2) for d in range(7)],
    }


# ------------------------------------------------------------------ main ----
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path) as fh:
            cfg.update(json.load(fh))
    return cfg


def render(data, template_path, out_path):
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"), help="transcripts root")
    ap.add_argument("--since", type=int, default=30, help="days back; 0 = everything")
    ap.add_argument("--config", default=os.path.join(here, "config.json") if os.path.exists(os.path.join(here, "config.json")) else None)
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "ccdash.html"))
    ap.add_argument("--json", metavar="FILE", help="write aggregated data as JSON instead of HTML")
    ap.add_argument("--open", action="store_true", help="open the result in the default browser")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.projects):
        sys.exit(f"transcripts root not found: {a.projects}")
    cfg = load_config(a.config)
    pricing = Pricing(cfg)
    data = aggregate(a.projects, cfg, a.since, pricing)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        print(f"wrote {a.json}")
        return
    render(data, os.path.join(here, "template.html"), a.out)
    t = data["totals"]
    print(f"wrote {a.out}: {t['requests']:,} requests, {t['sessions']} sessions, est. ${t['cost']:,.2f}, "
          f"cache hit {t['hit']*100:.1f}% (last {a.since or 'all'} days)")
    print(f"cache lifetime {data['meta']['ttl_minutes']} min ({data['meta']['ttl_source']})")
    if data["meta"]["unknown_models"]:
        print("unknown models priced as Sonnet 5 (add to config aliases/prices): " + ", ".join(data["meta"]["unknown_models"]))
    if a.open:
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    main()
