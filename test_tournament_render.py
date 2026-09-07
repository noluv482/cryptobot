#!/usr/bin/env python3
"""Contract checks for 2026-09-05: the dashboard RENDERS the tournament
statistics it already receives, and the spread map regenerates itself.

Why this exists: /autopilot shipped dsr/psr/min_trl/verdict/killed since the
DSR work (2026-09-04) but the served dashboard HTML contained none of those
tokens — the owner could not see the graveyard or why a row had no number.
And spread_hours.json was documented as "written by learning_report.py" with
nothing that ever ran it in the container, so the spread gate was a hard cap
only, forever.

Runs DB-less. No network. No side effects.

Usage:
    python test_tournament_render.py
"""
import io
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot_server as bs          # noqa: E402
import autopilot as ap           # noqa: E402

bs.log = lambda *a, **k: None
ap.log = lambda *a, **k: None
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


HTML = bs._DASHBOARD_HTML
SRC_BS = io.open(os.path.join(HERE, "bot_server.py"), encoding="utf-8").read()
SRC_AP = io.open(os.path.join(HERE, "autopilot.py"), encoding="utf-8").read()

# ── 1. the standings renderer shows the survival columns ─────────────────────
# The standings renderer starts at the anti-fooling helpers (apLabel /
# apCell / apN): they ARE part of the renderer contract — every stat cell
# is printed through them — so the segment the checks below read must
# cover them, not just the innerHTML builder underneath.
seg_start = HTML.find("const AP_LABELS=")
if seg_start < 0:
    seg_start = HTML.find("const st=$('ap_standings')")
seg_end = HTML.find("async function toggleAutopilot", seg_start)
SEG = HTML[seg_start:seg_end] if seg_start > 0 and seg_end > seg_start else ""
check("standings renderer segment found", bool(SEG))
for tok in ("r.dsr", "r.psr", "r.min_trl", "r.trades_n", "r.verdict",
            "r.status_note", "d.killed", "d.trials_count", "d.kill_psr"):
    check(f"renderer reads {tok}", tok in SEG)
check("KILLED rows are labelled, not hidden",
      "'KILLED'" in SEG and "opacity:.55" in SEG)
check("null survival stats render as a dash, never 0",
      "r.dsr!=null?r.dsr.toFixed(2):'—'" in SEG
      and "r.psr!=null?r.psr.toFixed(2):'—'" in SEG)
check("green still means CLEARS only (existing contract kept)",
      "r.clears_cost?'var(--g)'" in SEG)
check("weekly entrant kinds get a via tag (trend/carry/switch), not a bare id",
      "cf_trend:'trend'" in SEG and "cf_carry:'carry'" in SEG
      and "cf_switch:'switch'" in SEG)
check("served JS has no backslash in the new segment (non-raw string hazard)",
      "\\" not in SEG)

# ── 2. the payload carries status_note per standings row ─────────────────────
check("standings rows ship status_note",
      re.search(r'"standings":\s*\[.*?"status_note"', SRC_AP, re.S) is not None)
try:
    # the DB-less instance path is exercised in test_autopilot_tournament; here
    # only the field contract, on whatever an instance returns
    inst = ap.Autopilot() if hasattr(ap, "Autopilot") else None
    st = inst.status() if inst is not None else None
    if isinstance(st, dict) and st.get("standings"):
        check("status().standings[0] has status_note key",
              "status_note" in st["standings"][0])
except Exception as e:                      # constructor may want state files
    print(f"skip  live status() probe ({type(e).__name__}: {e})")

# ── 3. the spread map regenerates itself in-process ──────────────────────────
check("_spread_map_loop exists", hasattr(bs, "_spread_map_loop"))
check("Spread map thread is registered at boot",
      '("Spread map",        _spread_map_loop,     ()),' in SRC_BS)
loop_src = SRC_BS[SRC_BS.find("def _spread_map_loop"):SRC_BS.find("def _learning_filler_loop")]
check("hook writes to the SAME path the gate reads (SPREAD_HOURS_FILE)",
      "run_report(_conn, SPREAD_HOURS_FILE)" in loop_src)
check("hook uses DATABASE_URL from env, never a literal dsn",
      'os.environ.get("DATABASE_URL")' in loop_src
      and "postgres://" not in loop_src and "postgresql://" not in loop_src)
check("hook closes its connection", "_conn.close()" in loop_src)
check("hook never raises out of the thread",
      "except Exception as e:" in loop_src and "time.sleep(86400)" in loop_src)
check("learning_report stays read-only (no INSERT/UPDATE/DELETE)",
      not re.search(r"\b(INSERT|UPDATE|DELETE)\b",
                    io.open(os.path.join(HERE, "learning_report.py"),
                            encoding="utf-8").read().split("def _connect")[0]
                    .replace("Writes NOTHING", "")))

# ── 4. nothing here touches trading settings ─────────────────────────────────
check("PAPER_LOCK untouched by this work",
      "PAPER_LOCK" not in loop_src and "PAPER_LOCK" not in SEG)


# ═════════════════════════════════════════════════════════════════════════════
# 2026-09-06 EXTENSION — the ANTI-FOOLING renderer rule, the GOAL tile, the
# /api/goal seam and the tournament SSE kinds.
#
# THE RULE: any cell that prints sr / edge / psr must also print its n and
# exactly one of {insufficient data, n<MinTRL (verdict ~N mo), survives,
# KILLED, PROVEN}. A middle number with no sample size behind it is how a
# dashboard fools its owner, so this is not checked by grepping for tokens —
# the served renderer is EXECUTED under node against fixture standings, and
# every rendered cell is inspected. The checker is itself checked against a
# hand-built bad render, so a checker that stopped discriminating fails too.
# ═════════════════════════════════════════════════════════════════════════════
import html as _html
import json as _json
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile

AP_START = "const AP_LABELS="
SEG2_A = HTML.find(AP_START)
SEG2_B = HTML.find("async function toggleAutopilot", SEG2_A)
SEG2 = HTML[SEG2_A:SEG2_B] if SEG2_A > 0 and SEG2_B > SEG2_A else ""
check("anti-fooling renderer segment found (helpers + renderer)", bool(SEG2))
# the r.trades_n read moved into the apN helper — the segment above covers it
for tok in ("r.trades_n", "r.dsr", "r.psr", "r.min_trl", "r.verdict", "r.sr"):
    check(f"renderer segment reads {tok}", tok in SEG2)
check("the five labels are a fixed, declared vocabulary",
      "insufficient data" in SEG2 and "MinTRL" in SEG2 and "survives" in SEG2
      and "KILLED" in SEG2 and "PROVEN" in SEG2)
check("stat and edge numbers go through apCell (one chokepoint, not per-cell care)",
      SEG2.count("apCell(") >= 3 and "function apCell(" in SEG2)
check("apCell appends n and the label unconditionally (no optional branch)",
      "parts.concat(['n '+apN(r), apLabel(r)])" in SEG2)
check("served anti-fooling JS has no stray backslash (non-raw string hazard)",
      "\\" not in SEG2)

LABELS = ("insufficient data", "n<MinTRL", "survives", "KILLED", "PROVEN")
METRIC = re.compile(r"\b(SR|DSR|PSR|MinTRL)\s|[+-]\d+\.\d+%")
SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
ROWSPLIT = re.compile(r'<div style="margin-bottom:7px')


def _text(frag):
    return _html.unescape(re.sub(r"<[^>]+>", "", frag)).replace(" ", " ")


def audit_render(standings_html):
    """Every span that prints a stat must carry 'n <int>' and exactly one label.

    Returns [] when the render is honest, else one violation per offending
    cell. This is the executable form of the ANTI-FOOLING contract."""
    bad = []
    for row in ROWSPLIT.split(standings_html)[1:]:
        for frag in SPAN.findall(row):
            t = _text(frag).strip()
            if not METRIC.search(t):
                continue
            has_n = re.search(r"\bn \d+\b", t) is not None
            hits = [lab for lab in LABELS if lab in t]
            if not has_n or len(hits) != 1:
                bad.append({"cell": t[:90], "n": has_n, "labels": hits})
    return bad


FIX_ROWS = [
    # n below the promotion bar, nothing computed yet
    {"id": "hyp_seed", "via": "cf", "n_oos": 3, "oos_edge": None, "t": None,
     "sr": None, "dsr": None, "psr": None, "min_trl": None, "trades_n": 3,
     "clears_cost": False, "verdict": "insufficient data"},
    # a real number, but the track is too short for a verdict
    {"id": "cf_trend", "via": "cf_trend", "n_oos": 41, "oos_edge": 0.0021, "t": 2.4,
     "sr": 0.83, "dsr": 0.31, "psr": 0.62, "min_trl": 96.0, "trades_n": 41,
     "clears_cost": True, "verdict": "~7 months to verdict"},
    # survives
    {"id": "cf_carry", "via": "cf_carry", "n_oos": 120, "oos_edge": 0.0009, "t": 2.1,
     "sr": 0.55, "dsr": 0.18, "psr": 0.71, "min_trl": 40.0, "trades_n": 120,
     "clears_cost": True, "verdict": "SURVIVES the cost gate"},
    # killed
    {"id": "cf_switch", "via": "cf_switch", "n_oos": 88, "oos_edge": -0.0013, "t": -1.4,
     "sr": -0.4, "dsr": -0.9, "psr": 0.02, "min_trl": 30.0, "trades_n": 88,
     "clears_cost": False, "verdict": "KILLED", "killed": True,
     "status_note": "cost"},
    # proven
    {"id": "hyp_lead", "via": "cf", "n_oos": 260, "oos_edge": 0.0034, "t": 3.9,
     "sr": 1.02, "dsr": 0.71, "psr": 0.98, "min_trl": 24.0, "trades_n": 260,
     "clears_cost": True, "verdict": "PROVEN", "proven": True},
]
GOAL_BLOCK = {
    "proven": ["hyp_lead"], "alive": 3, "killed": 1, "trials_count": 14,
    "n_eff": 9.5, "sd_sr": 0.41, "sr0": 0.732,
    "nearest_verdict": {"id": "cf_trend", "months": 4.2},
    "families": {"trend": {"decisions_per_year": 52, "alive": ["cf_trend"],
                           "killed": [], "posterior": {"s": 1, "f": 2}}},
    "budget_remaining": 2, "book_state": "champion:hyp_lead",
}
FIXTURES = {
    "full": {"enabled": True, "allocation": "hyp_lead", "champion": "hyp_lead",
             "cost_gate_pct": 0.24, "challengers": ["a", "b"], "min_oos_trades": 20,
             "t_margin": 2, "trials_count": 14, "kill_psr": 0.05,
             "killed": {"cf_switch": {"reason": "cost: gross positive, net <= 0"}},
             "standings": FIX_ROWS, "goal": GOAL_BLOCK},
    "no_goal": {"enabled": True, "allocation": "FLAT", "min_oos_trades": 20,
                "trials_count": 7, "killed": {"a1": {"reason": "no_signal"},
                                              "a2": {"reason": "regime_flip"}},
                "standings": FIX_ROWS[:2]},
    "off": {"enabled": False, "boot_error": None},
    # nothing killed yet: the graveyard must SAY it is empty and why, not
    # print a bare 0 that reads like a result
    "empty_grave": {"enabled": True, "min_oos_trades": 20, "killed": {},
                    "standings": FIX_ROWS[:1],
                    "goal": dict(GOAL_BLOCK, killed=0, proven=[])},
    "nulls": {"enabled": True, "min_oos_trades": 20, "standings": [
        {"id": "bare", "n_oos": 55, "oos_edge": 0.001, "t": 2.0, "sr": 0.5,
         "dsr": None, "psr": None, "min_trl": None, "trades_n": None,
         "clears_cost": True, "verdict": ""}]},
}

_JSDIR = _tempfile.mkdtemp(prefix="ap_render_")
_JS = os.path.join(_JSDIR, "render.js")
PRELUDE = (
    "const _els={};\n"
    "function $(id){if(!_els[id])_els[id]={id:id,innerHTML:'',textContent:'',"
    "className:'',style:{}};return _els[id];}\n"
    "let _apEnabled=false;\n"
)
DRIVER = (
    "\nconst _fx=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));\n"
    "const _out={};\n"
    "for(const k of Object.keys(_fx)){\n"
    "  for(const id of Object.keys(_els))delete _els[id];\n"
    "  renderAutopilot(_fx[k]);\n"
    "  _out[k]={standings:$('ap_standings').innerHTML,goal:$('ap_goal').innerHTML,\n"
    "           graveyard_present:$('ap_goal').innerHTML.indexOf('GRAVEYARD')>=0};\n"
    "}\n"
    "process.stdout.write(JSON.stringify(_out));\n"
)
with io.open(_JS, "w", encoding="utf-8") as f:
    f.write(PRELUDE + SEG2 + DRIVER)
_FXP = os.path.join(_JSDIR, "fixtures.json")
with io.open(_FXP, "w", encoding="utf-8") as f:
    f.write(_json.dumps(FIXTURES))

RENDERED = {}
try:
    _r = _subprocess.run(["node", _JS, _FXP], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    if _r.returncode == 0:
        RENDERED = _json.loads(_r.stdout)
    else:
        print("   node stderr:", (_r.stderr or "")[:400])
except FileNotFoundError:
    _r = None
except Exception as _e:                          # noqa: BLE001 — reported, not raised
    _r = None
    print("   node run failed:", type(_e).__name__, _e)

if not RENDERED:
    check("the served renderer EXECUTES under node (anti-fooling rule is testable)",
          False, "node missing or renderer threw — see stderr above")
else:
    check("the served renderer executes under node against fixture standings", True)

    # ── the rule, row by row, on the real render ─────────────────────────────
    for fx in ("full", "no_goal", "nulls", "empty_grave"):
        viol = audit_render(RENDERED[fx]["standings"])
        check(f"[{fx}] every stat cell carries its n and exactly one label",
              not viol, viol[:2])

    S_FULL = RENDERED["full"]["standings"]
    T_FULL = _text(S_FULL)
    check("a below-bar row says 'insufficient data', it does not print an edge",
          "insufficient data" in T_FULL and "3/20" in T_FULL)
    check("a row short of MinTRL prints the months-to-verdict, not a verdict",
          re.search(r"n<MinTRL \(verdict ~7 mo\)", T_FULL) is not None, T_FULL[:200])
    check("a surviving row is labelled 'survives', never 'proven'",
          "survives" in T_FULL)
    check("CLEARS is spelled 'CLEARS (not proven)' so the badge cannot be misread",
          "CLEARS (not proven)" in T_FULL)
    _rows_full = ROWSPLIT.split(S_FULL)[1:]
    check("PROVEN appears on the proven row and nowhere else",
          len(_rows_full) == 5
          and all(("PROVEN" in r) == ("hyp_lead" in r) for r in _rows_full),
          [i for i, r in enumerate(_rows_full) if "PROVEN" in r])
    check("the KILLED row stays listed and dimmed, not hidden",
          "cf_switch" in T_FULL and "opacity:.55" in S_FULL)
    check("null survival stats still render as a dash, never a fabricated 0",
          "DSR — " in _text(RENDERED["nulls"]["standings"])
          or "DSR —" in _text(RENDERED["nulls"]["standings"]),
          _text(RENDERED["nulls"]["standings"])[:200])
    check("a row with n=null still prints an n (0), never an unlabelled number",
          not audit_render(RENDERED["nulls"]["standings"]))

    # ── the checker discriminates (a bad render MUST fail it) ────────────────
    BAD = ('<div style="margin-bottom:7px">'
           '<span class="mono">SR 0.83 &middot; DSR 0.31 &middot; PSR 0.62</span></div>')
    check("the audit FLAGS a stat cell with no n and no label",
          len(audit_render(BAD)) == 1, audit_render(BAD))
    BAD_N = ('<div style="margin-bottom:7px">'
             '<span class="mono">SR 0.83 &middot; n 41</span></div>')
    check("the audit FLAGS a stat cell that has n but no label",
          len(audit_render(BAD_N)) == 1)
    BAD_L = ('<div style="margin-bottom:7px">'
             '<span class="mono">SR 0.83 &middot; survives</span></div>')
    check("the audit FLAGS a stat cell that has a label but no n",
          len(audit_render(BAD_L)) == 1)
    BAD_2 = ('<div style="margin-bottom:7px">'
             '<span class="mono">SR 0.8 &middot; n 41 &middot; survives &middot; PROVEN</span></div>')
    check("the audit FLAGS a cell wearing two labels at once",
          len(audit_render(BAD_2)) == 1)

    # ── the GOAL tile ────────────────────────────────────────────────────────
    TILE = re.compile(r'sim-stat-lbl">([^<]*)</div><div class="sim-stat-val"[^>]*>([^<]*)<')

    def tiles(fx):
        return {k.strip(): _html.unescape(v).strip()
                for k, v in TILE.findall(RENDERED[fx]["goal"])}

    tf = tiles("full")
    for key in ("Proven", "Alive", "Killed", "Trials", "N_eff", "SR0 hurdle",
                "Nearest verdict", "Budget left", "Book"):
        check(f"GOAL tile shows {key}", key in tf, sorted(tf))
    check("GOAL proven names the entrant", tf.get("Proven") == "hyp_lead", tf.get("Proven"))
    check("GOAL alive/killed/trials/N_eff come from the block",
          (tf.get("Alive"), tf.get("Killed"), tf.get("Trials"), tf.get("N_eff"))
          == ("3", "1", "14", "9.5"), tf)
    check("GOAL prints the SR0 hurdle at full precision",
          tf.get("SR0 hurdle") == "0.732", tf.get("SR0 hurdle"))
    check("GOAL nearest verdict names the entrant AND the months",
          tf.get("Nearest verdict") == "cf_trend ~4.2 mo", tf.get("Nearest verdict"))
    check("GOAL shows the remaining trial budget and the book state",
          tf.get("Budget left") == "2" and tf.get("Book") == "champion:hyp_lead", tf)

    toff = tiles("off")
    check("with no goal block every unknown prints 'unknown', never 0",
          all(toff.get(k) == "unknown" for k in
              ("Proven", "Alive", "Killed", "Trials", "N_eff", "SR0 hurdle",
               "Nearest verdict", "Budget left", "Book")), toff)
    tng = tiles("no_goal")
    check("without a goal block the MEASURED counts still show (killed, trials)",
          tng.get("Killed") == "2" and tng.get("Trials") == "7", tng)
    check("...and the unmeasured ones stay 'unknown' rather than defaulting to 0",
          tng.get("Alive") == "unknown" and tng.get("SR0 hurdle") == "unknown", tng)

    # graveyard + trials are ALWAYS visible, autopilot on or off
    for fx in ("full", "no_goal", "off"):
        g = _text(RENDERED[fx]["goal"])
        check(f"[{fx}] GRAVEYARD and trials count are visible",
              "GRAVEYARD" in g and re.search(r"trials (\d+|unknown)", g) is not None,
              g[:160])
    gfull = _text(RENDERED["full"]["goal"])
    check("the graveyard names the killed entrant and its reason",
          "cf_switch" in gfull and "cost: gross positive" in gfull, gfull[:200])
    check("an empty graveyard SAYS it is empty and why, never a bare 0",
          "no entrant has yet reached MinTRL" in _text(RENDERED["empty_grave"]["goal"]),
          _text(RENDERED["empty_grave"]["goal"])[:200])
    check("an empty proven list reads 'none yet', not an empty string",
          tiles("empty_grave").get("Proven") == "none yet",
          tiles("empty_grave").get("Proven"))

    # reality strip: the standing expectations sentence
    for fx in ("full", "no_goal", "off"):
        g = _text(RENDERED[fx]["goal"])
        check(f"[{fx}] the reality strip carries the standing expectation",
              "REALITY:" in g and "most hypotheses die" in g, g[:120])
    check("the reality strip restates the anti-fooling rule and the paper limit",
          "carries its n and a label" in gfull and "Paper only" in gfull, gfull[-260:])
    check("the reality strip never claims profit",
          "profit" not in gfull.lower() or "no profit claim" in gfull.lower())

    # the goal block renders even with the allocator OFF (the point of the tile)
    check("the GOAL tile renders with the autopilot OFF",
          bool(RENDERED["off"]["goal"].strip()))
    check("...and says the block is unavailable rather than inventing one",
          "goal block unavailable" in _text(RENDERED["off"]["goal"]))

_shutil.rmtree(_JSDIR, ignore_errors=True)

# ── /api/goal [G] ────────────────────────────────────────────────────────────
check("/api/goal is registered on the flask app",
      "/api/goal" in {str(r) for r in bs._flask_app.url_map.iter_rules()})
_view = bs._flask_app.view_functions.get(
    next((r.endpoint for r in bs._flask_app.url_map.iter_rules() if str(r) == "/api/goal"), ""))
check("/api/goal resolves to a view function", callable(_view))

GOAL_KEYS = ("proven", "alive", "killed", "trials_count", "n_eff", "sd_sr", "sr0",
             "nearest_verdict", "families", "budget_remaining", "book_state")


class _FakeAP:
    def __init__(self, payload, boom=False):
        self._p, self._boom = payload, boom

    def status(self):
        if self._boom:
            raise RuntimeError("status exploded")
        return self._p


_saved_ap = bs._autopilot
try:
    bs._autopilot = None
    r = _view()
    body = _json.loads(r.get_data(as_text=True))
    check("autopilot disabled -> 404 with goal:null and a reason, never an exception",
          r.status_code == 404 and body.get("goal") is None and body.get("reason"),
          (r.status_code, body))

    bs._autopilot = _FakeAP({"goal": GOAL_BLOCK})
    r = _view()
    body = _json.loads(r.get_data(as_text=True))
    check("a computed goal block is served verbatim under 'goal'",
          r.status_code == 200 and body.get("goal") == GOAL_BLOCK, r.status_code)
    check("the served block carries every [G] key",
          all(k in (body.get("goal") or {}) for k in GOAL_KEYS),
          sorted(body.get("goal") or {}))
    check("/api/goal is served no-store (the HUD never caches a verdict)",
          r.headers.get("Cache-Control") == "no-store", dict(r.headers))

    bs._autopilot = _FakeAP({"enabled": True})          # older build, no goal yet
    r = _view()
    body = _json.loads(r.get_data(as_text=True))
    check("a build without the goal block returns goal:null + reason, not {}",
          r.status_code == 404 and body.get("goal") is None and body.get("reason"),
          (r.status_code, body))

    bs._autopilot = _FakeAP(None, boom=True)
    r = _view()
    body = _json.loads(r.get_data(as_text=True))
    check("status() raising becomes a 404 with a reason, never a 500",
          r.status_code == 404 and body.get("goal") is None
          and "status()" in str(body.get("reason")), (r.status_code, body))

    bs._autopilot = _FakeAP("not a dict")
    r = _view()
    check("a garbage status() shape is refused, not forwarded",
          r.status_code == 404)
finally:
    bs._autopilot = _saved_ap

_goal_src = SRC_BS[SRC_BS.find('@_flask_app.route("/api/goal")'):SRC_BS.find("def _research_compact")]
check("the /api/goal view never writes state or calls an order path",
      not re.search(r"\b(open\(|json\.dump\(|POST|place_order|_private)\b", _goal_src),
      _goal_src[:80])
check("the /api/goal view never fabricates a block when one is missing",
      "goal not computed" in _goal_src or "not computed by this autopilot build" in _goal_src)

# ── [S] SSE kinds -> event spine ─────────────────────────────────────────────
SSE_CASES = [
    ("autopilot_register",
     {"entrant": "hyp_lead", "origin": "llm_prereg", "family": "lead_lag",
      "born_ts": 1788600000.0, "trials_count": 14, "n_eff": 9.5},
     "cryptobot.lab.registered",
     ("entrant", "origin", "family", "born_ts", "trials_count", "n_eff")),
    ("autopilot_switch",
     {"from": "cf_trend", "to": "hyp_lead", "why": "higher deflated SR at n>=20"},
     "cryptobot.tournament.switch", ("from", "to", "why")),
    ("autopilot_clears",
     {"entrant": "cf_carry", "n": 120, "sr": 0.55, "sr0": 0.73},
     "cryptobot.tournament.clears", ("entrant", "n", "sr", "sr0")),
    ("autopilot_proven",
     {"entrant": "hyp_lead", "dsr": 0.71, "n": 260, "min_trl": 24.0},
     "cryptobot.goal.proven", ("entrant", "dsr", "n", "min_trl")),
]
for etype, payload, kind, keys in SSE_CASES:
    got = bs._sse_to_event(etype, payload)
    check(f"{etype} maps to a typed event", isinstance(got, tuple) and len(got) == 3, got)
    if not (isinstance(got, tuple) and len(got) == 3):
        continue
    k, text, data = got
    check(f"{etype} -> {kind}", k == kind, k)
    check(f"{etype} carries every payload key into data",
          all(key in data and data[key] == payload[key] for key in keys), data)
    check(f"{etype} text is one honest sentence (<=300 chars, no newline)",
          isinstance(text, str) and 0 < len(text) <= 300 and "\n" not in text, text)
    check(f"{etype} text names the entrant it is about",
          str(payload.get("entrant") or payload.get("to") or "") in text, text)
    check(f"{etype} data is JSON-serialisable and under 2KB",
          len(_json.dumps(data)) < 2048)

check("a 'clears' event is not allowed to read as a 'proven'",
      "not proven" in bs._sse_to_event("autopilot_clears",
                                       {"entrant": "x", "n": 30, "sr": 1, "sr0": 0.5})[1])
check("a 'proven' event says it is a paper record, not a profit claim",
      "paper" in bs._sse_to_event("autopilot_proven",
                                  {"entrant": "x", "dsr": 1, "n": 30,
                                   "min_trl": 2})[1].lower())
check("a register event says the hurdle rises (a trial is a cost, not a win)",
      "hurdle" in bs._sse_to_event("autopilot_register", {"entrant": "x"})[1])
check("a switch event names both sides and says it is the paper book",
      "paper book" in bs._sse_to_event("autopilot_switch",
                                       {"from": None, "to": "x", "why": "w"})[1])
check("the existing kill mapping still works (no regression)",
      bs._sse_to_event("autopilot_kill", {"entrant": "z", "reason": "cost"})[0]
      == "cryptobot.tournament.kill")
for etype, payload, kind, _keys in SSE_CASES:
    partial = bs._sse_to_event(etype, {})
    check(f"{etype} with an empty payload still maps, with 'unknown'/None, never a crash",
          isinstance(partial, tuple) and partial[0] == kind, partial)

# ── nothing in this seam can reach money ─────────────────────────────────────
check("the goal/SSE seam never mentions PAPER_LOCK or an order path",
      "PAPER_LOCK" not in SEG2 and "PAPER_LOCK" not in _goal_src
      and not re.search(r"place_order|_private\(", SEG2 + _goal_src))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all tournament-render / spread-map hook checks pass")
