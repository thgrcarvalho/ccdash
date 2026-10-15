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


def assistant(mid, model, ts, inp=0, cw=0, cr=0, out=10, cw1=0, tool_uses=()):
    content = [{"type": "text", "text": "ok"}] + [{"type": "tool_use", "id": tid, "name": name, "input": {}} for tid, name in tool_uses]
    return line(type="assistant", timestamp=ts, message={
        "id": mid, "model": model, "content": content,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
                  "cache_read_input_tokens": cr, "cache_creation": {"ephemeral_1h_input_tokens": cw1},
                  "output_tokens_details": {"thinking_tokens": 4}}})


def tool_result(tid, text, ts, tur=None):
    d = dict(type="user", timestamp=ts, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]})
    if tur is not None:
        d["toolUseResult"] = tur
    return line(**d)


class CcdashTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        proj = os.path.join(self.root, "-home-me-proj")
        os.makedirs(os.path.join(proj, "sess1", "subagents"))
        t = ["2026-10-10T10:00:00Z", "2026-10-10T10:00:30Z", "2026-10-10T10:01:00Z", "2026-10-10T12:00:00Z"]
        with open(os.path.join(proj, "sess1.jsonl"), "w") as fh:
            fh.write(line(type="user", timestamp=t[0], message={"role": "user", "content": "hello"}))
            fh.write(assistant("m1", "claude-sonnet-5-5", t[0], cw=24000, out=50, tool_uses=[("tu1", "Bash")]))
            fh.write(assistant("m1", "claude-sonnet-5-5", t[0], cw=24000, out=80, tool_uses=[("tu1", "Bash")]))  # streamed duplicate
            fh.write(tool_result("tu1", "x" * 3500, t[1], tur={"stdout": "x" * 3500, "stderr": ""}))
            fh.write(assistant("m2", "claude-sonnet-5-5", t[1], cw=1200, cr=20000, out=20, tool_uses=[("tu2", "mcp__splunk__search")]))
            fh.write(tool_result("tu2", "y" * 7000, t[2]))
            fh.write(assistant("m3", "claude-sonnet-5-5", t[2], cw=2100, cr=21200, out=20))
            # resumed after 2 h on a 5-minute TTL: full rewrite
            fh.write(line(type="user", timestamp=t[3], message={"role": "user", "content": "more"}))
            fh.write(assistant("m4", "claude-sonnet-5-5", t[3], cw=23400, cr=0, out=20))
        with open(os.path.join(proj, "sess1", "subagents", "agent-1.jsonl"), "w") as fh:
            fh.write(assistant("s1", "claude-haiku-5-5", t[1], cw=5000, out=30))

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_agg(self, **cfg):
        c = dict(ccdash.DEFAULT_CONFIG); c.update(cfg)
        return ccdash.aggregate(self.root, c, 0, ccdash.Pricing(c))

    def test_dedup_and_totals(self):
        d = self.run_agg(ttl_minutes=5)
        self.assertEqual(d["totals"]["requests"], 5)             # 4 main (m1 once) + 1 subagent
        self.assertEqual(d["totals"]["out"], 80 + 20 + 20 + 20 + 30)
        self.assertEqual(d["totals"]["cw"], 24000 + 1200 + 2100 + 23400 + 5000)
        self.assertEqual(d["totals"]["sessions"], 1)
        self.assertEqual(d["sessions"][0]["subagents"], 1)

    def test_pricing(self):
        p = ccdash.Pricing({"aliases": {"gw-sonnet": "claude-sonnet-5-5"}, "price_multiplier": 1.1})
        self.assertAlmostEqual(p.cost("gw-sonnet", inp=1e6), 2.2)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", cr=1e6), 0.22)
        self.assertAlmostEqual(p.cost("claude-sonnet-5", cw5=1e6, cw1=1e6), 1.1 * (2.5 + 4.0))
        p.resolve("mystery-model")
        self.assertIn("mystery-model", p.unknown)

    def test_cache_rewrite_classes(self):
        d = self.run_agg(ttl_minutes=5)
        self.assertEqual(d["misses"]["first"]["n"], 1)
        self.assertEqual(d["misses"]["idle"]["n"], 1)      # m4 after a 2 h gap
        self.assertEqual(d["misses"]["breaker"]["n"], 0)
        d60 = self.run_agg(ttl_minutes=180)
        self.assertEqual(d60["misses"]["idle"]["n"], 0)      # same gap, inside a 3 h lifetime
        self.assertEqual(d60["misses"]["breaker"]["n"], 1)

    def test_tool_attribution(self):
        d = self.run_agg()
        tools = {t["tool"]: t for t in d["tools"]}
        self.assertEqual(tools["Bash"]["calls"], 1)
        self.assertAlmostEqual(tools["Bash"]["direct"], 1000, delta=1)
        # Bash result is followed by m2, m3 and m4 before any context drop: carried = direct x 2
        self.assertAlmostEqual(tools["Bash"]["carried"], 2000, delta=2)
        self.assertEqual(d["servers"][0]["server"], "splunk")
        self.assertAlmostEqual(d["servers"][0]["direct"], 2000, delta=1)

    def test_render(self):
        d = self.run_agg()
        out = os.path.join(self.root, "x.html")
        ccdash.render(d, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "template.html"), out)
        html = open(out, encoding="utf-8").read()
        self.assertIn('"tools":', html)
        self.assertNotIn("/*__DATA__*/null", html)


if __name__ == "__main__":
    unittest.main()
