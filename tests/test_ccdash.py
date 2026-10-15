import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ccdash  # noqa: E402


def line(**kw):
    return json.dumps(kw) + "\n"


def assistant(mid, model, ts, inp=0, cw=0, cr=0, out=10, one_hour=False, breakdown=True, tool_uses=()):
    content = [{"type": "text", "text": "ok"}] + [{"type": "tool_use", "id": tid, "name": name, "input": {}} for tid, name in tool_uses]
    usage = {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
             "cache_read_input_tokens": cr, "output_tokens_details": {"thinking_tokens": 4}}
    if breakdown:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": cw if one_hour else 0, "ephemeral_5m_input_tokens": 0 if one_hour else cw}
    return line(type="assistant", timestamp=ts, message={"id": mid, "model": model, "content": content, "usage": usage})


def tool_result(tid, text, ts, tur=None):
    d = dict(type="user", timestamp=ts, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]})
    if tur is not None:
        d["toolUseResult"] = tur
    return line(**d)


def write_session(root, one_hour=False, breakdown=True):
    """One main session: 4 requests, a Bash and an MCP result, a 30-minute pause before the last request."""
    proj = os.path.join(root, "-home-me-proj")
    os.makedirs(os.path.join(proj, "sess1", "subagents"), exist_ok=True)
    t = ["2026-10-10T10:00:00Z", "2026-10-10T10:00:30Z", "2026-10-10T10:01:00Z", "2026-10-10T10:31:00Z"]
    kw = dict(one_hour=one_hour, breakdown=breakdown)
    with open(os.path.join(proj, "sess1.jsonl"), "w") as fh:
        fh.write(line(type="user", timestamp=t[0], message={"role": "user", "content": "hello"}))
        fh.write(assistant("m1", "claude-sonnet-5-5", t[0], cw=24000, out=50, tool_uses=[("tu1", "Bash")], **kw))
        fh.write(assistant("m1", "claude-sonnet-5-5", t[0], cw=24000, out=80, tool_uses=[("tu1", "Bash")], **kw))  # streamed duplicate
        fh.write(tool_result("tu1", "x" * 3500, t[1], tur={"stdout": "x" * 3500, "stderr": ""}))
        fh.write(assistant("m2", "claude-sonnet-5-5", t[1], cw=1200, cr=24000, out=20, tool_uses=[("tu2", "mcp__splunk__search")], **kw))
        fh.write(tool_result("tu2", "y" * 7000, t[2]))
        fh.write(assistant("m3", "claude-sonnet-5-5", t[2], cw=2100, cr=25200, out=20, **kw))
        # 30 minutes later: full rewrite (idle on a 5-minute cache, a prefix breaker on a 60-minute one)
        fh.write(line(type="user", timestamp=t[3], message={"role": "user", "content": "more"}))
        fh.write(assistant("m4", "claude-sonnet-5-5", t[3], cw=27300, cr=0, out=20, **kw))
    with open(os.path.join(proj, "sess1", "subagents", "agent-1.jsonl"), "w") as fh:
        fh.write(assistant("s1", "claude-haiku-5-5", t[1], cw=5000, out=30, **kw))


class CcdashTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_agg(self, **cfg):
        c = dict(ccdash.DEFAULT_CONFIG)
        c.update(cfg)
        return ccdash.aggregate(self.root, c, 0, ccdash.Pricing(c))

    def test_dedup_and_totals(self):
        write_session(self.root)
        d = self.run_agg()
        self.assertEqual(d["totals"]["requests"], 5)             # 4 main (m1 once) + 1 subagent
        self.assertEqual(d["totals"]["out"], 80 + 20 + 20 + 20 + 30)
        self.assertEqual(d["totals"]["cw"], 24000 + 1200 + 2100 + 27300 + 5000)
        self.assertEqual(d["totals"]["sessions"], 1)
        self.assertEqual(d["sessions"][0]["subagents"], 1)

    def test_pricing(self):
        p = ccdash.Pricing({"aliases": {"gw-sonnet": "claude-sonnet-5-5"}, "price_multiplier": 1.1})
        self.assertAlmostEqual(p.cost("gw-sonnet", inp=1e6), 2.2)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", cr=1e6), 0.22)
        self.assertAlmostEqual(p.cost("claude-sonnet-5", cw5=1e6, cw1=1e6), 1.1 * (2.5 + 4.0))
        p.resolve("mystery-model")
        self.assertIn("mystery-model", p.unknown)

    def test_rewrite_classes_with_explicit_ttl(self):
        write_session(self.root)
        d5 = self.run_agg(ttl_minutes=5)
        self.assertEqual((d5["misses"]["first"]["n"], d5["misses"]["idle"]["n"], d5["misses"]["breaker"]["n"]), (1, 1, 0))
        d60 = self.run_agg(ttl_minutes=60)
        self.assertEqual((d60["misses"]["idle"]["n"], d60["misses"]["breaker"]["n"]), (0, 1))
        self.assertEqual(d60["meta"]["ttl_source"], "from config")

    def test_ttl_auto_detect(self):
        write_session(self.root, one_hour=False)
        d = self.run_agg(ttl_minutes="auto")
        self.assertEqual(d["meta"]["ttl_minutes"], 5)
        self.assertEqual(d["misses"]["idle"]["n"], 1)
        shutil.rmtree(self.root); os.makedirs(self.root)
        write_session(self.root, one_hour=True)
        d = self.run_agg(ttl_minutes="auto")
        self.assertEqual(d["meta"]["ttl_minutes"], 60)
        self.assertEqual(d["misses"]["breaker"]["n"], 1)
        self.assertTrue(d["meta"]["ttl_source"].startswith("observed"))
        shutil.rmtree(self.root); os.makedirs(self.root)
        write_session(self.root, breakdown=False)        # gateway dropped the breakdown
        d = self.run_agg(ttl_minutes="auto")
        self.assertEqual(d["meta"]["ttl_minutes"], 5)
        self.assertTrue(d["meta"]["ttl_source"].startswith("assumed"))

    def test_whatif_other_lifetime(self):
        # 5-minute cache observed: the 30-minute pause rewrite (27,300 tok) disappears under a 1-hour cache,
        # every other main write is repriced from 1.25x to 2x input (Sonnet 5.5: $2.5 -> $4 per MTok)
        write_session(self.root, one_hour=False)
        d = self.run_agg()
        w = d["whatif"]
        self.assertEqual((w["ttl_now"], w["ttl_alt"]), (5, 60))
        self.assertAlmostEqual(w["write_cost_now"], (24000 + 1200 + 2100 + 27300) * 2.5 / 1e6, places=4)
        self.assertAlmostEqual(w["write_cost_alt"], (24000 + 1200 + 2100) * 4 / 1e6, places=4)
        self.assertEqual([g["n"] for g in d["idle_gaps"]], [0, 1, 0, 0, 0])      # one idle rewrite, 15-60 min bucket
        shutil.rmtree(self.root); os.makedirs(self.root)
        write_session(self.root, one_hour=True)
        d = self.run_agg()
        w = d["whatif"]
        self.assertEqual((w["ttl_now"], w["ttl_alt"]), (60, 5))
        self.assertAlmostEqual(w["write_cost_now"], 54600 * 4 / 1e6, places=4)
        self.assertAlmostEqual(w["write_cost_alt"], 54600 * 2.5 / 1e6, places=4)   # m4 is already a full rewrite (breaker)
        self.assertEqual(w["extra_rewrites"], 0)

    def test_tool_attribution(self):
        write_session(self.root)
        d = self.run_agg()
        tools = {t["tool"]: t for t in d["tools"]}
        self.assertEqual(tools["Bash"]["calls"], 1)
        self.assertAlmostEqual(tools["Bash"]["direct"], 1000, delta=1)
        # Bash result is followed by m2, m3 and m4 before any context drop: carried = direct x 2
        self.assertAlmostEqual(tools["Bash"]["carried"], 2000, delta=2)
        self.assertEqual(d["servers"][0]["server"], "splunk")
        self.assertAlmostEqual(d["servers"][0]["direct"], 2000, delta=1)

    def test_render(self):
        write_session(self.root)
        d = self.run_agg()
        out = os.path.join(self.root, "x.html")
        ccdash.render(d, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "template.html"), out)
        html = open(out, encoding="utf-8").read()
        self.assertIn('"tools":', html)
        self.assertNotIn("/*__DATA__*/null", html)


if __name__ == "__main__":
    unittest.main()
