#!/usr/bin/env python3
"""research_pass.py contract — every remote/LLM/rig step mocked, executed.

Pins: the [H] sanitizer (ids, kinds, cf whitelist, born_ts never earlier,
cf_only forced, slot cap), Thompson picks (B=2), the full happy path (events
in contract [P] order, atomic tmp+mv upload through the container uid, [R]
state), LLM fallbacks (junk / unavailable / local-only -> template), inbox
precedence + honest inexpressible answer, budget-exhausted, idempotence per
week, sanitize gate, rig gate (pbo > 0.20, unknown), dry-run (no ssh writes,
no events), evidence-unavailable stop, HYP_MAX_SLOTS, and the safety scan (no
order-placing symbol, no PAPER_LOCK, no manual_lab in the new files).
"""
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RESEARCH_PASS_NO_LLM"] = "1"          # never touch the real chain in tests
import research_pass as rp                         # noqa: E402

FAILS = []
T0 = 1_788_800_000.0                               # 2026-09-07 UTC (a Monday, ISO 2026-W37)


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── fixtures ─────────────────────────────────────────────────────────────────
def goal_block(budget=3, trend=(50, 0), carry=(40, 0), rest=(0, 50)):
    fams = {}
    for f in rp.FAMILIES:
        s, fl = {"trend": trend, "carry": carry}.get(f, rest)
        fams[f] = {"decisions_per_year": 52, "alive": [], "killed": [],
                   "posterior": {"s": s, "f": fl}}
    return {"proven": [], "alive": 5, "killed": 3, "trials_count": 12, "n_eff": 9,
            "sd_sr": 0.4, "sr0": 0.9, "nearest_verdict": {"id": "tsmom_btc_20w", "months": 5.5},
            "families": fams, "budget_remaining": budget, "book_state": "flat"}


def evidence_pack(goal=None, graveyard=None):
    return {"generated": "2026-09-07T05:30:00", "regime_table": {"trend": 0.4},
            "gate_table": {"cost": 12}, "spread_map_summary": {"tier": "6h"},
            "rejects_by_gate_week": {}, "shadow_counts": {"rows": 1200},
            "graveyard": graveyard if graveyard is not None else [
                {"id": "lab_r1s1", "family": "reversion_pattern", "kind": "price",
                 "horizon": "fwd48", "cost_model": "spot", "reason_code": "psr_kill",
                 "sr": -0.3, "sr0": 0.9, "dsr": -1.1, "n": 24, "killed_ts": T0 - 86400,
                 "cf": {"conf": 0.60, "rr": None, "adx": None, "er": None, "horizon": "fwd6"}}],
            "funding_summary": {"PF_XBTUSD": {"ann_7d": 0.08}},
            "tca_summary": {"rt_cost": 0.0026},
            "goal": goal if goal is not None else goal_block()}


class Fakes:
    """Recorder for every seam. remote_hyps=None -> `cat` fails like a missing file."""

    def __init__(self, pack=None, remote_hyps=None, evidence_rc=0, data_writable=False):
        self.pack, self.remote_hyps = pack, remote_hyps
        self.evidence_rc, self.data_writable = evidence_rc, data_writable
        self.ssh, self.scp, self.posts, self.rig_calls, self.prompts = [], [], [], [], []

    def SSH(self, cmd):
        self.ssh.append(cmd)
        if "research_evidence.py" in cmd:
            if self.evidence_rc != 0:
                return self.evidence_rc, "", "python: can't open file 'research_evidence.py'"
            return 0, "INF DB connected\n" + json.dumps(self.pack), ""
        if cmd.startswith("cat "):
            if self.remote_hyps is None:
                return 1, "", "cat: No such file"
            return 0, json.dumps(self.remote_hyps), ""
        if cmd.startswith("test -w"):
            return (0 if self.data_writable else 1), "", ""
        return 0, "", ""

    def SCP(self, local, remote):
        self.scp.append((local, remote, open(local, encoding="utf-8").read()))
        return 0

    def POST(self, url, payload):
        self.posts.append((url, payload))
        return True

    def FIND_SIGNAL(self, cf, cost, venue):
        self.rig_calls.append((dict(cf), cost, venue))
        return {"pbo": 0.12, "deoverlap_edge": 0.0011, "survivor": True, "n": 80}


def cloud_brain(label="groq:gpt-oss-120b", recorder=None):
    def fn(prompt, **kw):
        if recorder is not None:
            recorder.prompts.append(prompt)
        fam = re.search(r"FAMILY: (\w+)", prompt).group(1)
        idea = "OWNER IDEA" in prompt
        kind = rp.FAMILY_KIND.get(fam)
        if idea and "lead" in prompt.lower() and "alts" in prompt.lower():
            return {"expressible": False, "why_not": "needs simultaneous multi-pair rows"}, label
        if idea and "funding" in prompt.lower():
            kind, fam = "carry", "carry"
        cf = {"trend": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 30, "horizon": "fwd168"},
              "carry": {"symbol": "PF_ETHUSD"},
              "price": {"conf": 0.70, "rr": 2.5, "adx": None, "er": None, "horizon": "fwd24"}}.get(kind)
        if cf is None:
            return {"expressible": False, "why_not": "no cf expression for this family"}, label
        return {"id": f"hyp_llm_{fam[:6]}", "kind": kind, "family": fam, "cf": cf,
                "born_ts": T0 - 999999,                     # earlier than intake: must be ignored
                "note": "HYPOTHESIS: llm mechanism paragraph",
                "prereg": {"mechanism": "llm mechanism", "expected_decisions_per_month": 4,
                           "mintrl_estimate_months": 6, "kill_bar": "PSR<0.05",
                           "cost_model": "spot RT 0.26%"}}, label
    return fn


def fresh(fk, brain=None, sanitize=None, clock=T0, seed=7):
    d = tempfile.mkdtemp(prefix="rp-")
    rp.DATA_DIR = d
    rp.SSH, rp.SCP, rp.POST, rp.FIND_SIGNAL = fk.SSH, fk.SCP, fk.POST, fk.FIND_SIGNAL
    rp.BRAIN, rp.SANITIZE = brain, sanitize
    rp.CLOCK = (lambda: clock)
    rp.RNG = random.Random(seed)
    rp._HYP_MAX_SLOTS = None
    return d


def kinds(res):
    return [e["kind"] for e in res["events"]]


def ev(res, kind):
    return [e for e in res["events"] if e["kind"] == kind]


# ── 1. sanitizer contract [H] ────────────────────────────────────────────────
rp.DATA_DIR = tempfile.mkdtemp(prefix="rp-san-")
good = {"kind": "trend", "family": "trend", "origin": "llm_prereg",
        "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 30, "horizon": "fwd168",
               "conf": 0.9, "evil": 1},
        "note": "mechanism without prefix", "born_ts": T0 - 5000, "cf_only": False,
        "prereg": {"mechanism": "m", "expected_decisions_per_month": 4,
                   "mintrl_estimate_months": 6, "kill_bar": "k", "cost_model": "c"}}
out = rp.local_sanitize({"hyp_ok": good}, intake_ts=T0)
check("sanitize: valid entry survives", "hyp_ok" in out)
e = out.get("hyp_ok", {})
check("sanitize: cf whitelisted per kind (price keys + junk dropped from trend)",
      set(e.get("cf", {})) == {"rule", "pair", "weeks", "horizon"}, e.get("cf"))
check("sanitize: born_ts = intake time, never earlier", e.get("born_ts") == T0, e.get("born_ts"))
check("sanitize: cf_only forced True", e.get("cf_only") is True)
check("sanitize: note gets the HYPOTHESIS: prefix", str(e.get("note", "")).startswith("HYPOTHESIS:"))
later = rp.local_sanitize({"hyp_ok": {**good, "born_ts": T0 + 3600}}, intake_ts=T0)["hyp_ok"]
check("sanitize: a later born_ts is kept (only earlier is refused)", later["born_ts"] == T0 + 3600)
bad = {
    "badid": good, "hyp_UPPER": good, "hyp_" + "x" * 25: good,
    "hyp_kind": {**good, "kind": "live"},
    "hyp_fam": {**good, "family": "alpha"},
    "hyp_orig": {**good, "origin": "machine"},
    "hyp_rule": {**good, "cf": {"rule": "sweep", "pair": "XBTUSD", "weeks": 4}},
    "hyp_noprereg": {**good, "prereg": {"mechanism": "m"}},
    "hyp_nonote": {**good, "note": ""},
    "hyp_base": {**good, "kind": "price", "cf": {"horizon": "fwd48"}},
    "hyp_horz": {**good, "kind": "price", "cf": {"conf": 0.6, "horizon": "48h"}},
}
out = rp.local_sanitize(bad, intake_ts=T0)
check("sanitize: every malformed entry dropped (id regex, kind, family, origin, rule, prereg, note, base-dup, horizon)",
      out == {}, list(out))
many = {f"hyp_s{i}": good for i in range(8)}
check("sanitize: HYP_MAX_SLOTS cap (5)", len(rp.local_sanitize(many, T0)) == 5)
check("sanitize: bad file never raises -> {}", rp.local_sanitize("junk", T0) == {}
      and rp.local_sanitize([1, 2], T0) == {})
check("sanitize: carry needs symbol / switch needs pair+symbol+weeks",
      rp.local_sanitize({"hyp_c": {**good, "kind": "carry", "cf": {}}}, T0) == {}
      and "hyp_c" in rp.local_sanitize({"hyp_c": {**good, "kind": "carry", "cf": {"symbol": "PF_XBTUSD"}}}, T0)
      and rp.local_sanitize({"hyp_w": {**good, "kind": "switch", "cf": {"pair": "XBTUSD"}}}, T0) == {})

# ── 2. Thompson pick ─────────────────────────────────────────────────────────
post = rp.posterior_for(rp.load_state(), goal_block())
picks = rp.local_pick_families(post, 2, random.Random(3))
check("pick: B=2 distinct families with theta", len(picks) == 2
      and len({p["family"] for p in picks}) == 2 and all(0 <= p["theta"] <= 1 for p in picks))
check("pick: strong posteriors win (trend + carry)", {p["family"] for p in picks} == {"trend", "carry"}, picks)
check("pick: bot posterior outranks local tally", post["trend"]["from_bot"] and post["trend"]["s"] == 50)

# ── 3. happy path with a cloud LLM ───────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={"hyp_old": {"kind": "carry", "family": "carry",
                                                              "cf": {"symbol": "PF_XBTUSD"}}})
d = fresh(fk, brain=cloud_brain(recorder=fk))
res = rp.run_pass()
check("happy: not stopped", res["stopped"] is None, res["stopped"])
check("happy: evidence file written [E]", os.path.exists(os.path.join(d, "evidence", "evidence_2026-09-07.json")))
check("happy: two picks emitted with theta + reason",
      len(ev(res, "cryptobot.lab.pick")) == 2
      and all(e["data"].get("theta") is not None and e["data"].get("reason") for e in ev(res, "cryptobot.lab.pick")))
check("happy: two hypotheses submitted", len(res["submitted"]) == 2, res)
check("happy: rig event per candidate; carry skipped (graded live) with cost_model stated",
      len(ev(res, "cryptobot.lab.rig")) == 2
      and any("skipped" in e["text"] and e["data"]["pass"] for e in ev(res, "cryptobot.lab.rig")))
import bot_server as _bs                            # noqa: E402  (PC import: no DB, no threads)
check("happy: trend rig ran find_signal at the spot cost from the repo constant (ROUND_TRIP_COST_PCT)",
      len(fk.rig_calls) == 1 and fk.rig_calls[0][2] == "spot"
      and fk.rig_calls[0][1] == float(_bs.ROUND_TRIP_COST_PCT), fk.rig_calls)
check("happy: submitted events carry id/family/origin",
      all(e["data"].get("id") and e["data"].get("family") and e["data"].get("origin") == "llm_prereg"
          for e in ev(res, "cryptobot.lab.submitted")))
order = kinds(res)
check("happy: event order evidence -> pick -> rig -> submitted -> summary",
      order.index("cryptobot.lab.evidence") < order.index("cryptobot.lab.pick")
      < order.index("cryptobot.lab.rig") < order.index("cryptobot.lab.submitted")
      < order.index("cryptobot.lab.summary") and order.count("cryptobot.lab.summary") == 1)
check("happy: every event posted to the spine as system cryptobot with dotted kind",
      len(fk.posts) == len(res["events"]) and all(p["system"] == "cryptobot"
                                                  and re.match(r"^[a-z_]+(\.[a-z_]+)+$", p["kind"])
                                                  and len(p["text"]) <= 300 for _, p in fk.posts))
local = json.load(open(os.path.join(d, "hypotheses.json"), encoding="utf-8"))
check("happy: local hypotheses.json = remote current + new (3 entries, cf_only, born_ts=intake)",
      set(local) == {"hyp_old", "hyp_llm_trend", "hyp_llm_carry"}
      and all(local[k]["cf_only"] is True and local[k]["born_ts"] == T0 for k in ("hyp_llm_trend", "hyp_llm_carry")),
      local.keys())
check("happy: no private key '_source' leaks into the file",
      not any(k.startswith("_") for v in local.values() for k in v))
check("happy: atomic upload = scp to stage + container cat>tmp && mv (host cannot write data dir)",
      len(fk.scp) == 1 and fk.scp[0][1].endswith("/.hypotheses.json.upload")
      and any("docker exec -i cryptobot-bot-1 sh -c 'cat > /data/.hypotheses.json.tmp && mv -f" in c
              for c in fk.ssh), fk.ssh[-1:])
check("happy: uploaded bytes == local file", json.loads(fk.scp[0][2]) == local)
st = json.load(open(os.path.join(d, "hypothesis_budget.json"), encoding="utf-8"))
check("happy: [R] state week + families trials/last_pick + picks",
      st["week"] == "2026-W37" and st["families"]["trend"]["trials"] == 1
      and st["families"]["trend"]["last_pick_ts"] == T0 and st["picks"][-1]["done"] is True
      and st["picks"][-1]["submitted"] == res["submitted"], st)
check("happy: LLM prompt carries graveyard + schema + evidence + never-repeat rule",
      fk.prompts and all("GRAVEYARD" in p and "SCHEMA" in p and "EVIDENCE SUMMARY" in p
                         and "never repeat" in p for p in fk.prompts))
check("happy: summary text names picks, registered ids and the measured budget",
      "hyp_llm_trend" in ev(res, "cryptobot.lab.summary")[0]["text"]
      and "budget_remaining was 3" in ev(res, "cryptobot.lab.summary")[0]["text"])
check("happy: log file written", os.path.exists(os.path.join(d, "research_pass.log")))

# ── 4. idempotence ───────────────────────────────────────────────────────────
n_ssh = len(fk.ssh)
res2 = rp.run_pass()
check("idempotent: second run in the same week stops with no ssh, no events",
      res2["stopped"] == "already_done" and len(fk.ssh) == n_ssh and res2["events"] == [])
res3 = rp.run_pass(force=True)
check("idempotent: --force reruns (remote already has the ids -> nothing re-submitted)",
      res3["stopped"] is None and len(fk.ssh) > n_ssh)
rp.CLOCK = lambda: T0 + 7 * 86400
res4 = rp.run_pass()
check("idempotent: next ISO week runs again", res4["stopped"] is None and res4["week"] == "2026-W38")

# ── 5. LLM fallbacks -> template ─────────────────────────────────────────────
def junk_brain(prompt, **kw):
    return {"kind": "live", "cf": "yes", "id": "!!"}, "groq:gpt-oss-120b"


fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=junk_brain)
res = rp.run_pass()
check("fallback junk: template supplies the spec, still submitted",
      len(res["submitted"]) == 2 and all(sid.startswith("hyp_") for sid in res["submitted"]), res["submitted"])
check("fallback junk: template ids differ from LLM ids", not any("llm" in s for s in res["submitted"]))


def dead_brain(prompt, **kw):
    raise RuntimeError("all crew brains failed JSON: groq: 429")


fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=dead_brain)
res = rp.run_pass()
check("fallback unavailable: template path submits", len(res["submitted"]) == 2, res)

fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain(label="local", recorder=fk))
res = rp.run_pass()
check("fallback local-only: the chain's 'local' label is NOT treated as cloud -> template",
      len(res["submitted"]) == 2 and not any("llm" in s for s in res["submitted"]), res["submitted"])
check("no-LLM marker: brain_available() is None under RESEARCH_PASS_NO_LLM", rp.BRAIN is not None
      and (setattr(rp, "BRAIN", None) or rp.brain_available() is None))

# template exhaustion on a second week -> next untested parameterization
fk = Fakes(pack=evidence_pack(), remote_hyps={})
d = fresh(fk, brain=None)
r1 = rp.run_pass()
rp.CLOCK = lambda: T0 + 7 * 86400
fk.remote_hyps = json.load(open(os.path.join(d, "hypotheses.json"), encoding="utf-8"))
r2 = rp.run_pass()
tr1 = [e["data"]["cf"] for e in ev(r1, "cryptobot.lab.submitted") if e["data"]["family"] == "trend"]
tr2 = [e["data"]["cf"] for e in ev(r2, "cryptobot.lab.submitted") if e["data"]["family"] == "trend"]
check("template: next week gets the NEXT untested parameterization", tr1 and tr2 and tr1 != tr2, (tr1, tr2))

# ── 6. graveyard distance ────────────────────────────────────────────────────
grave = [{"id": "dead_tsmom", "family": "trend", "kind": "trend",
          "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 10, "horizon": "fwd168"}}]
check("graveyard: identical spec is too close", rp.too_close("trend", grave[0]["cf"], grave, {}) is not None)
check("graveyard: within 10% numeric is too close",
      rp.too_close("trend", {**grave[0]["cf"], "weeks": 11}, grave, {}) is not None)
check("graveyard: >10% away is new", rp.too_close("trend", {**grave[0]["cf"], "weeks": 20}, grave, {}) is None)
check("graveyard: different pair is new", rp.too_close("trend", {**grave[0]["cf"], "pair": "ETHUSD"}, grave, {}) is None)
fk = Fakes(pack=evidence_pack(graveyard=grave), remote_hyps={})
fresh(fk, brain=None)
res = rp.run_pass()
tcf = [e["data"]["cf"] for e in ev(res, "cryptobot.lab.submitted") if e["data"]["family"] == "trend"]
check("graveyard: the killed tsmom_10w template is skipped, next one submitted",
      tcf and tcf[0].get("weeks") != 10, tcf)


def repeat_brain(prompt, **kw):
    # The note must clear the sanitizer (>=10 chars of mechanism after the prefix)
    # or the spec dies there and never reaches the graveyard distance check —
    # which is the thing this case is pinning.
    return {"id": "hyp_repeat", "kind": "trend", "family": "trend", "cf": grave[0]["cf"],
            "note": "HYPOTHESIS: re-proposes the already-killed tsmom 10w spec",
            "prereg": {"mechanism": "re-proposes a killed spec",
                       "expected_decisions_per_month": 4,
                       "mintrl_estimate_months": 6, "kill_bar": "k",
                       "cost_model": "c"}}, "gemini:gemini-2.5-flash"


fk = Fakes(pack=evidence_pack(graveyard=grave), remote_hyps={})
fresh(fk, brain=repeat_brain)
res = rp.run_pass()
check("graveyard: an LLM spec that repeats a killed spec is dropped with an event",
      "hyp_repeat" not in res["submitted"]
      and any("within 10% of killed dead_tsmom" in e["text"] for e in ev(res, "cryptobot.lab.dropped")))

# ── 7. owner inbox precedence ────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(goal=goal_block(budget=2)), remote_hyps={})
d = fresh(fk, brain=cloud_brain(recorder=fk))
json.dump([{"ts": T0 - 100, "text": "what about lead lag between BTC and the alts", "source": "telegram"},
           {"ts": T0 - 200, "text": "harvest funding carry on ETH perp", "source": "telegram"}],
          open(os.path.join(d, "hypotheses_inbox.json"), "w", encoding="utf-8"))
res = rp.run_pass()
subs = ev(res, "cryptobot.lab.submitted")
check("inbox: owner idea takes a slot first (origin owner_idea, carry)",
      subs and subs[0]["data"]["origin"] == "owner_idea" and subs[0]["data"]["family"] == "carry", subs[:1])
check("inbox: the oldest idea is handled first (ts order)", "funding" in fk.prompts[0])
check("inbox: budget 2 = 1 owner idea + 1 sampled family",
      len(res["submitted"]) == 2 and sum(e["data"]["origin"] == "llm_prereg" for e in subs) == 1)
sug = ev(res, "cryptobot.lab.suggested")
check("inbox: inexpressible idea answered honestly (event text says why)",
      len(sug) == 1 and "not registered" in sug[0]["text"] and "multi-pair" in sug[0]["text"], sug)
arch = json.load(open(os.path.join(d, "hypotheses_inbox_archive.json"), encoding="utf-8"))
check("inbox: both ideas archived with outcome, inbox emptied",
      len(arch) == 2 and {a["id"] is not None for a in arch} == {True, False}
      and json.load(open(os.path.join(d, "hypotheses_inbox.json"), encoding="utf-8")) == [])

# no-LLM inbox: keyword mapping + honest refusal
fk = Fakes(pack=evidence_pack(goal=goal_block(budget=2)), remote_hyps={})
d = fresh(fk, brain=None)
json.dump([{"ts": 1, "text": "buy when the moon is full", "source": "telegram"},
           {"ts": 2, "text": "try a donchian breakout on ETH", "source": "telegram"}],
          open(os.path.join(d, "hypotheses_inbox.json"), "w", encoding="utf-8"))
res = rp.run_pass()
check("inbox no-LLM: unmappable idea refused with a reason",
      any("no family keyword" in e["text"] for e in ev(res, "cryptobot.lab.suggested")))
check("inbox no-LLM: mappable idea -> template of that family, origin owner_idea",
      any(e["data"]["origin"] == "owner_idea" and e["data"]["family"] == "trend"
          for e in ev(res, "cryptobot.lab.submitted")), ev(res, "cryptobot.lab.submitted"))

# ── 8. budget exhausted ──────────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(goal=goal_block(budget=0)), remote_hyps={})
d = fresh(fk, brain=cloud_brain())
res = rp.run_pass()
check("budget 0: stops with budget_exhausted event + ONE summary, no picks, no scp",
      res["stopped"] == "budget_exhausted" and kinds(res) == ["cryptobot.lab.evidence", "cryptobot.lab.budget_exhausted",
                                                              "cryptobot.lab.summary"]
      and fk.scp == [] and res["picks"] == [], kinds(res))
check("budget 0: event carries trials_count + sr0 from the goal block",
      ev(res, "cryptobot.lab.budget_exhausted")[0]["data"] == {"trials_count": 12, "sr0": 0.9})
check("budget 0: week marked done (idempotent)", rp.week_done(rp.load_state(), "2026-W37"))
g = goal_block(); g.pop("budget_remaining")
fk = Fakes(pack=evidence_pack(goal=g), remote_hyps={})
fresh(fk, brain=cloud_brain())
res = rp.run_pass()
check("budget unknown: refuses to pick (unknown beats a guess)", res["stopped"] == "budget_unknown"
      and ev(res, "cryptobot.lab.budget_exhausted")[0]["data"]["budget_remaining"] == "unknown")

# ── 9. sanitize gate ─────────────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain(), sanitize=lambda raw: {})
res = rp.run_pass()
check("sanitize gate: the bot's sanitizer rejecting -> dropped events, nothing submitted, no scp",
      res["submitted"] == [] and fk.scp == []
      and sum("failed sanitize_hypotheses" in e["text"] for e in ev(res, "cryptobot.lab.dropped")) == 2)


def raising_sanitizer(raw):
    raise ValueError("boom")


fresh(Fakes(pack=evidence_pack(), remote_hyps={}), brain=cloud_brain(), sanitize=raising_sanitizer)
check("sanitize gate: a raising sanitizer never raises out (-> {})", rp.sanitize({"hyp_x": good}) == {})
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain(), sanitize=lambda raw: [{"id": k, **v, "cf_only": True} for k, v in raw.items()])
res = rp.run_pass()
check("sanitize gate: list-shaped sanitizer output accepted", len(res["submitted"]) == 2)

# ── 10. rig gate ─────────────────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain())
fk.FIND_SIGNAL = lambda cf, cost, venue: {"pbo": 0.35, "deoverlap_edge": 0.002, "survivor": True}
rp.FIND_SIGNAL = fk.FIND_SIGNAL
res = rp.run_pass()
rig = ev(res, "cryptobot.lab.rig")
check("rig: pbo 0.35 > 0.20 -> trend NOT submitted (carry still is)",
      res["submitted"] == ["hyp_llm_carry"] and any("FAIL" in e["text"] and e["data"]["pbo"] == 0.35 for e in rig), res)
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain())
rp.FIND_SIGNAL = lambda cf, cost, venue: {"pbo": 0.1, "deoverlap_edge": -0.0004, "survivor": False}
res = rp.run_pass()
check("rig: no de-overlap survivor -> not submitted", "hyp_llm_trend" not in res["submitted"])
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain())
rp.FIND_SIGNAL = lambda cf, cost, venue: {"error": "find_signal produced no JSON (--json unsupported?)"}
res = rp.run_pass()
check("rig: unknown result (no --json yet) -> honest 'unknown' and not submitted",
      "hyp_llm_trend" not in res["submitted"]
      and any("unknown" in e["text"] and e["data"]["pbo"] is None for e in ev(res, "cryptobot.lab.rig")))
check("rig: PBO_MAX is the house rule 0.20 (never loosened)", rp.PBO_MAX == 0.20)
check("rig: _extract_json finds the trailing JSON after prose",
      rp._extract_json("fetching 3 pairs ...\n{\"a\": {\"b\": 1}}\n") == {"a": {"b": 1}}
      and rp._extract_json("no json here") is None)


def carry_no_cost(prompt, **kw):
    d, l = cloud_brain()(prompt)
    if d.get("kind") == "carry":
        d["prereg"]["cost_model"] = ""
    return d, l


fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=carry_no_cost)
res = rp.run_pass()
check("rig: carry without cost_model is refused (sanitizer/rig) — only trend submitted",
      res["submitted"] == ["hyp_llm_trend"], res["submitted"])

# ── 11. dry-run ──────────────────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={})
d = fresh(fk, brain=cloud_brain())
json.dump([{"ts": 1, "text": "funding carry on ETH", "source": "t"}],
          open(os.path.join(d, "hypotheses_inbox.json"), "w", encoding="utf-8"))
res = rp.run_pass(dry_run=True)
check("dry-run: events recorded locally but NOT posted", len(res["events"]) > 3 and fk.posts == [])
check("dry-run: no scp, no ssh writes (only evidence + cat reads)",
      fk.scp == [] and all(c.startswith("docker exec cryptobot-bot-1 python research_evidence.py")
                           or c.startswith("cat ") for c in fk.ssh), fk.ssh)
check("dry-run: no state, no local hypotheses file, inbox untouched",
      not os.path.exists(os.path.join(d, "hypothesis_budget.json"))
      and not os.path.exists(os.path.join(d, "hypotheses.json"))
      and len(json.load(open(os.path.join(d, "hypotheses_inbox.json"), encoding="utf-8"))) == 1)
check("dry-run: evidence file still written (a read, not a remote write)",
      os.path.exists(os.path.join(d, "evidence", "evidence_2026-09-07.json")))

# ── 12. evidence unavailable ─────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={}, evidence_rc=2)
fresh(fk, brain=cloud_brain())
res = rp.run_pass()
check("evidence missing: stops honestly, no picks, no scp, event says why",
      res["stopped"] == "evidence_unavailable" and res["picks"] == [] and fk.scp == []
      and "unavailable" in ev(res, "cryptobot.lab.evidence")[0]["text"])
check("evidence missing: week NOT marked done (retry next run)", not rp.week_done(rp.load_state(), "2026-W37"))

# ── 13. HYP_MAX_SLOTS ────────────────────────────────────────────────────────
full = {f"hyp_f{i}": {"kind": "carry", "family": "carry", "cf": {"symbol": "PF_XBTUSD"}} for i in range(5)}
fk = Fakes(pack=evidence_pack(), remote_hyps=full)
fresh(fk, brain=cloud_brain())
res = rp.run_pass()
check("slots: 5/5 registered -> nothing submitted, no upload, no eviction",
      res["submitted"] == [] and fk.scp == [])
fk = Fakes(pack=evidence_pack(goal=goal_block(budget=5)), remote_hyps=dict(list(full.items())[:4]))
fresh(fk, brain=cloud_brain())
res = rp.run_pass()
up = json.loads(fk.scp[0][2]) if fk.scp else {}
check("slots: 4/5 -> exactly one accepted, file has 5", len(res["submitted"]) == 1 and len(up) == 5, res["submitted"])
check("slots: hyp_max_slots() never exceeds 5", rp.hyp_max_slots() <= 5)

# ── 14. direct-write path when the data dir IS writable ──────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={}, data_writable=True)
fresh(fk, brain=cloud_brain())
res = rp.run_pass()
check("upload: writable data dir -> scp .tmp into data dir then mv over ssh",
      fk.scp and fk.scp[0][1] == "~/cryptobot/data/.hypotheses.json.tmp"
      and any(c.startswith("mv -f ~/cryptobot/data/.hypotheses.json.tmp ~/cryptobot/data/hypotheses.json") for c in fk.ssh))

# ── 15. safety scan of the new files ─────────────────────────────────────────
FORBIDDEN = ("_kraken_place_order", "_kf_place_order", "_binance_place_order", "_kraken_private",
             "_kf_private", "_binance_private", "PAPER_LOCK", "manual_lab", "AddOrder", "sendOrder")
for fn in ("research_pass.py", "research_pass.ps1"):
    src = io.open(os.path.join(HERE, fn), encoding="utf-8").read()
    check(f"safety: {fn} has no order-placing / PAPER_LOCK / manual_lab symbol",
          not any(s in src for s in FORBIDDEN))
src = io.open(os.path.join(HERE, "research_pass.py"), encoding="utf-8").read()
check("safety: no direct Telegram code (announce goes through the spine)",
      "api.telegram.org" not in src and "sendMessage" not in src and "TG_TOKEN" not in src)
check("safety: every remote command is a read, evidence run, or the tmp+mv write",
      not re.search(r"docker (restart|compose|stop|start)|systemctl|git push|reboot", src))
ps1 = io.open(os.path.join(HERE, "research_pass.ps1"), encoding="utf-8").read()
check("ps1: runs the WindowsApps python and logs to data\\research_pass.log",
      "Microsoft\\WindowsApps\\python.exe" in ps1 and "research_pass.log" in ps1 and "research_pass.py" in ps1)

# ── 16. CLI ──────────────────────────────────────────────────────────────────
fk = Fakes(pack=evidence_pack(), remote_hyps={})
fresh(fk, brain=cloud_brain())
buf = io.StringIO()
old = sys.stdout
sys.stdout = buf
try:
    rc = rp.main(["--dry-run", "--once"])
finally:
    sys.stdout = old
check("cli: --dry-run --once exits 0 and prints a JSON result line",
      rc == 0 and json.loads(buf.getvalue().strip().splitlines()[-1])["dry_run"] is True)

print()
print(f"{len(FAILS)} failures" if FAILS else "ALL PASS")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)
