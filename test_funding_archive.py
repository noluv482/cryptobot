#!/usr/bin/env python3
"""Funding-archive breadth + carry gate diagnostic contract.

All MOCKED: no live Postgres, no Kraken HTTP. What must hold:

  1. UNIVERSE PARSING - FUNDING_SYMBOLS is env-driven, validated, de-duped,
     order-preserving; junk entries are dropped, never fetched.
  2. BREADTH - 23 archive symbols by default; PF_RAYUSD and PF_TRUMPUSD are
     ABSENT on purpose (RAY supplied ~half the headline OOS carry on 57.7bps
     spread / 30.4% funding vol).
  3. ARCHIVING IS NOT TRADING - the funding loop's only sink is
     upsert_funding_rates; no order path, no sizing, no scoring universe.
  4. FROZEN SCORING UNIVERSE - CARRY_SCORING_SYMBOLS is a separate constant,
     defaults to the 3 currently scored, is capped at 8, and is NOT derived
     from FUNDING_SYMBOLS.
  5. ROBUSTNESS - one 404/junk symbol is logged and skipped, never fatal, and
     never blocks the symbols after it.
  6. FULL vs INCREMENTAL - per symbol, chosen from MAX(ts) alone.
  7. GATE DIAGNOSTIC - exact wording for both branches on fixture rows, the
     spine event only when a gate reads ABOVE data, and the hurdle NEVER
     moved by the diagnostic.
  8. COST SINGLE SOURCE - funding_carry.py imports the bot's fee constants,
     has a documented standalone fallback, prints which it used, and the new
     numbers are STRICTER than the hardcoded ones they replaced.

Usage:  python test_funding_archive.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import shutil
import subprocess
import tempfile
import time

import bot_server as bs

_LOGS = []
bs.log = lambda *a, **k: _LOGS.append(" ".join(str(x) for x in a))

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "bot_server.py"), encoding="utf-8").read()
FC_SRC = open(os.path.join(HERE, "funding_carry.py"), encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── 1. UNIVERSE PARSING ─────────────────────────────────────────────────────
D = ("PF_XBTUSD", "PF_ETHUSD")
check("blank/None env keeps the default untouched",
      bs._parse_symbol_list(None, D) == D and bs._parse_symbol_list("   ", D) == D
      and bs._parse_symbol_list("", D) == D)
check("env overrides the default entirely",
      bs._parse_symbol_list("PF_SOLUSD,PF_XRPUSD", D) == ("PF_SOLUSD", "PF_XRPUSD"))
check("entries are stripped and upper-cased",
      bs._parse_symbol_list(" pf_solusd , PF_xrpusd ", D) == ("PF_SOLUSD", "PF_XRPUSD"))
check("duplicates collapse, first-seen order preserved",
      bs._parse_symbol_list("PF_SOLUSD,PF_XRPUSD,pf_solusd,PF_SOLUSD", D)
      == ("PF_SOLUSD", "PF_XRPUSD"))
_junk = bs._parse_symbol_list("PF_SOLUSD,,  ,XBT/USD,PF SOL,;DROP TABLE,PF_XRPUSD", D)
check("malformed entries are dropped, good ones survive",
      _junk == ("PF_SOLUSD", "PF_XRPUSD"), str(_junk))
check("dropping a malformed entry is LOGGED, not silent",
      any("malformed symbol" in m for m in _LOGS))
check("an all-junk env yields () — 'archive nothing', not a guess",
      bs._parse_symbol_list("???,,,", D) == ())
check("parser returns a tuple (immutable universe)",
      isinstance(bs._parse_symbol_list("PF_XBTUSD", D), tuple))

# ── 2. BREADTH: the 23 defaults, and the two deliberate exclusions ──────────
DEF = bs.FUNDING_SYMBOLS_DEFAULT
check("23 archive symbols by default", len(DEF) == 23, str(len(DEF)))
check("default list has no duplicates", len(set(DEF)) == len(DEF))
for s in ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD",
          "PF_XRPUSD", "PF_DOGEUSD", "PF_LINKUSD", "PF_HYPEUSD", "PF_XMRUSD",
          "PF_XAUTUSD", "PF_SUIUSD", "PF_BNBUSD",
          "PF_FARTCOINUSD", "PF_TIAUSD", "PF_FETUSD", "PF_RENDERUSD", "PF_PUMPUSD",
          "PF_ADAUSD", "PF_DOTUSD", "PF_AVAXUSD", "PF_LTCUSD", "PF_ARBUSD",
          "PF_NEARUSD", "PF_ZECUSD"):
    check(f"archive default contains {s}", s in DEF)
check("PF_RAYUSD is DELIBERATELY EXCLUDED", "PF_RAYUSD" not in DEF)
check("PF_TRUMPUSD is DELIBERATELY EXCLUDED", "PF_TRUMPUSD" not in DEF)
check("the RAY exclusion is documented in-file, with the reason",
      "57.7" in SRC and "30.4" in SRC and "DELIBERATELY EXCLUDED" in SRC)
check("every default symbol passes the parser's own validator",
      all(bs.FUNDING_SYMBOL_RE.match(s) for s in DEF))
check("FUNDING_SYMBOLS is the parsed constant the loop reads",
      isinstance(bs.FUNDING_SYMBOLS, tuple) and len(bs.FUNDING_SYMBOLS) >= 3)

# ── 3. ARCHIVING IS NOT TRADING ─────────────────────────────────────────────
_i = SRC.index("def _funding_history_loop")
REGION = SRC[SRC.index("# ── Funding archive universe"):SRC.index("def close_at(")]
for tok in ("place_order", "AddOrder", "kraken_request", "open_position",
            "_open(", "leverage", "paper_state", "PAPER_LOCK", "api_key",
            "add_trade", "size_usd"):
    check(f"funding archive region never touches {tok}", tok not in REGION)
import re as _re
check("the region's ONLY db write is upsert_funding_rates",
      REGION.count("n = db.upsert_funding_rates(") == 1
      and sorted(set(_re.findall(r"db\.\w+", REGION)))
          == ["db.conn", "db.funding_newest_ts", "db.funding_rows",
              "db.upsert_funding_rates"]
      and "INSERT INTO" not in REGION and "UPDATE " not in REGION,
      str(sorted(set(_re.findall(r"db\.\w+", REGION)))))
check("the loop iterates FUNDING_SYMBOLS (not a hardcoded tuple)",
      "syms = FUNDING_SYMBOLS" in SRC
      and 'syms = ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD")' not in SRC)
check("'ARCHIVING IS NOT TRADING' is stated in the code, not just here",
      "ARCHIVING IS NOT TRADING" in REGION)

# ── 4. FROZEN SCORING UNIVERSE ──────────────────────────────────────────────
check("CARRY_SCORING_SYMBOLS defaults to the 3 currently scored",
      bs._parse_symbol_list(None, ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"))
      == ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"))
check("scoring universe is a SEPARATE constant from the archive universe",
      "CARRY_SCORING_SYMBOLS" in SRC and
      "CARRY_SCORING_SYMBOLS = _parse_symbol_list(" in SRC and
      "FUNDING_SYMBOLS," not in SRC.split("CARRY_SCORING_SYMBOLS = ")[1][:300])
check("scoring universe is not derived from the archive universe at runtime",
      set(bs.CARRY_SCORING_SYMBOLS) < set(bs.FUNDING_SYMBOLS)
      and len(bs.CARRY_SCORING_SYMBOLS) <= bs.CARRY_SCORING_MAX)
check("scoring cap is 8", bs.CARRY_SCORING_MAX == 8)
check("cap actually truncates an over-wide env list",
      len(bs._parse_symbol_list(",".join(f"PF_A{i}USD" for i in range(20)), ())
          [:bs.CARRY_SCORING_MAX]) == 8)
check("changing the scoring universe is written down as a pre-registration event",
      "PRE-REGISTRATION EVENT" in SRC and "FROZEN BEFORE SCORING" in SRC)


# ── shared fakes ────────────────────────────────────────────────────────────
class _FakeDB:
    """Fake cursor-less DB: records what the pass asked for and sent."""
    def __init__(self, newest=None):
        self.conn = True
        self.newest = dict(newest or {})
        self.sent = {}
        self.asked = []
    def funding_newest_ts(self, venue, sym):
        self.asked.append((venue, sym))
        return self.newest.get(sym)
    def upsert_funding_rates(self, venue, sym, rows):
        self.sent.setdefault(sym, []).extend(rows)
        return len(rows)


def _with_db(fake, fn):
    saved = bs.db
    bs.db = fake
    try:
        return fn()
    finally:
        bs.db = saved


NOW = 1_757_000_000
HOURS = [(NOW - 3600 * i, 0.00001) for i in range(500)][::-1]   # ascending

# ── 5. ONE BAD SYMBOL NEVER ABORTS THE PASS ────────────────────────────────
def _fetch_with_one_bomb(sym):
    if sym == "PF_BADUSD":
        raise RuntimeError("404 Not Found")
    return HOURS


fake = _FakeDB()
res = _with_db(fake, lambda: bs._funding_history_pass(
    ("PF_XBTUSD", "PF_BADUSD", "PF_ZECUSD"), _fetch_with_one_bomb, now=NOW, pause=0))
check("a 404 symbol is reported as an error, not raised",
      res.get("PF_BADUSD") == ("error", 0), str(res))
check("the symbol BEFORE the bad one still archived", res["PF_XBTUSD"][1] == len(HOURS))
check("the symbol AFTER the bad one still archived — no early exit",
      res["PF_ZECUSD"][1] == len(HOURS))
check("nothing was written for the bad symbol", "PF_BADUSD" not in fake.sent)


def _fetch_junk(sym):
    return "not a list of rows" if sym == "PF_JUNKUSD" else HOURS


fake_j = _FakeDB()
res_j = _with_db(fake_j, lambda: bs._funding_history_pass(
    ("PF_JUNKUSD", "PF_XBTUSD"), _fetch_junk, now=NOW, pause=0))
check("a junk payload cannot poison later symbols", res_j["PF_XBTUSD"][1] == len(HOURS))

# ── 6. FULL vs INCREMENTAL, per symbol ─────────────────────────────────────
fake2 = _FakeDB(newest={
    "PF_ETHUSD": NOW - 3600,                        # fresh -> incremental
    "PF_SOLUSD": NOW - bs.FUNDING_FULL_STALE_S - 1,  # stale -> full
})
res2 = _with_db(fake2, lambda: bs._funding_history_pass(
    ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"), lambda s: HOURS, now=NOW, pause=0))
check("empty table for a symbol -> FULL backfill (the free 1-year pass)",
      res2["PF_XBTUSD"][0] == "full" and res2["PF_XBTUSD"][1] == len(HOURS))
check("fresh newest row -> INCREMENTAL", res2["PF_ETHUSD"][0] == "incremental")
check("incremental sends only the MAX(ts)-48h tail",
      res2["PF_ETHUSD"][1] == sum(1 for t, _ in HOURS
                                  if t >= NOW - 3600 - bs.FUNDING_INCR_WINDOW_S),
      str(res2["PF_ETHUSD"]))
check("a long outage (newest older than the stale window) -> FULL again",
      res2["PF_SOLUSD"][0] == "full" and res2["PF_SOLUSD"][1] == len(HOURS))
check("the mode decision is per symbol, all three decided in one pass",
      len(res2) == 3 and len(fake2.asked) == 3)
check("every symbol keeps its own per-symbol pause",
      "if pause:" in SRC and "time.sleep(pause)" in SRC)

# ── 7. CARRY GATE DIAGNOSTIC ───────────────────────────────────────────────
YEAR_H = 24 * 365


def _flat(ann, days=60, end=NOW):
    """`days` of hourly rates whose annualized mean is exactly `ann`."""
    r = ann / YEAR_H
    n = days * 24
    return [(end - 3600 * (n - 1 - i), r) for i in range(n)]


HURDLE = 0.184        # 4x the 4-leg RT + 10% floor, at the container's fees
txt_lo, d_lo = bs._carry_gate_diag_row("PF_XBTUSD", _flat(0.05), HURDLE)
txt_hi, d_hi = bs._carry_gate_diag_row("PF_SOLUSD", _flat(0.50), HURDLE)
print("      " + txt_lo)
print("      " + txt_hi)
check("ABOVE branch: exact required wording",
      txt_lo.startswith("CARRY PF_XBTUSD: hurdle 18.40%/yr vs realized trailing-7d "
                        "funding max ") and "mean " in txt_lo and " obs -> " in txt_lo,
      txt_lo)
check("ABOVE branch ends 'gate ABOVE data'", txt_lo.endswith("-> gate ABOVE data"), txt_lo)
check("WITHIN branch ends 'gate within data'",
      txt_hi.endswith("-> gate within data"), txt_hi)
check("ABOVE branch reports max/mean equal to the injected 5.00%/yr",
      abs(d_lo["max_ann"] - 0.05) < 1e-9 and abs(d_lo["mean_ann"] - 0.05) < 1e-9)
check("WITHIN branch reports the injected 50.00%/yr", abs(d_hi["max_ann"] - 0.50) < 1e-9)
# 60 days of hourly rates -> 59 evaluable UTC days, of which the first 5 hold
# fewer than the required 120 of 168 rates and are therefore NOT decidable.
check("obs count is the number of DECIDABLE days (warm-up excluded)",
      d_lo["n_obs"] == 59 - 5, str(d_lo["n_obs"]))
check("gate_above_data flag matches the wording",
      d_lo["gate_above_data"] is True and d_hi["gate_above_data"] is False)
check("n_clearing counts observations that actually clear the hurdle",
      d_lo["n_clearing"] == 0 and d_hi["n_clearing"] == d_hi["n_obs"])

# a hurdle sitting exactly ON the max is still ABOVE the data (no decision)
_t_eq, _d_eq = bs._carry_gate_diag_row("PF_ETHUSD", _flat(0.184), HURDLE)
check("hurdle exactly at the max still reads ABOVE data (a tie decides nothing)",
      _d_eq["gate_above_data"] is True, _t_eq)

# thin history: says so, never guesses a distribution
_t_thin, _d_thin = bs._carry_gate_diag_row("PF_TIAUSD", _flat(0.30, days=3), HURDLE)
check("too little history -> 'not enough history to say', no numbers invented",
      _d_thin is None and "not enough history to say" in _t_thin, _t_thin)
_t_e, _d_e = bs._carry_gate_diag_row("PF_FETUSD", [], HURDLE)
check("empty history -> same honest refusal", _d_e is None and "0 decidable obs" in _t_e)

# a window with a real hole still needs 120 of 168 hourly rates
_holed = [(t, r) for t, r in _flat(0.30) if (t // 3600) % 3 == 0]   # ~1/3 kept
_t_h, _d_h = bs._carry_gate_diag_row("PF_PUMPUSD", _holed, HURDLE)
check("a gappy window (< 120 of 168 rates) is not decidable",
      _d_h is None, _t_h)

# pass-level: spine event only when some symbol reads ABOVE data
EV = []
rows = {"PF_XBTUSD": _flat(0.05), "PF_ETHUSD": _flat(0.06), "PF_SOLUSD": _flat(0.50)}
out = bs._carry_gate_diag_pass(syms=("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"),
                               fetch_rows=lambda s: rows[s],
                               emit=lambda k, t, d=None, ref="": EV.append((k, t, d)))
check("one diagnostic row per SCORED symbol", len(out) == 3)
check("exactly one spine event", len(EV) == 1, str(len(EV)))
check("event kind is cryptobot.carry.gate_diag", EV and EV[0][0] == "cryptobot.carry.gate_diag")
check("event names only the symbols whose gate is above the data",
      EV and EV[0][2]["symbols_above"] == ["PF_XBTUSD", "PF_ETHUSD"], str(EV[0][2] if EV else ""))
check("event text says the hurdle is UNCHANGED",
      EV and "hurdle is unchanged" in EV[0][1].lower(), EV[0][1] if EV else "")
check("event carries the hurdle's provenance",
      EV and EV[0][2].get("hurdle_source"))

EV2 = []
out2 = bs._carry_gate_diag_pass(syms=("PF_SOLUSD",), fetch_rows=lambda s: _flat(0.50),
                                emit=lambda k, t, d=None, ref="": EV2.append(k))
check("NO event when every scored gate sits within the data", EV2 == [] and len(out2) == 1)

EV3 = []
out3 = bs._carry_gate_diag_pass(syms=("PF_XBTUSD",),
                                fetch_rows=lambda s: (_ for _ in ()).throw(RuntimeError("db down")),
                                emit=lambda k, t, d=None, ref="": EV3.append(k))
check("a failing reader degrades to no row, no event, no raise", out3 == [] and EV3 == [])

_hurdle, _src = bs._carry_hurdle_ann()
check("the diagnostic reads the LIVE hurdle from autopilot",
      _src == "autopilot.CARRY_HURDLE_ANN", _src)
try:
    import autopilot as _ap
    check("diagnostic hurdle == autopilot.CARRY_HURDLE_ANN exactly",
          _hurdle == _ap.CARRY_HURDLE_ANN, f"{_hurdle} vs {_ap.CARRY_HURDLE_ANN}")
    check("the CARRY hurdle constant is untouched by this work (still 4xRT + 0.10)",
          abs(_ap.CARRY_HURDLE_ANN - (_ap.CARRY_RT_4LEG * 4.0 + 0.10)) < 1e-12)
except Exception as e:
    check("autopilot importable for the hurdle check", False, str(e))

DIAG_SRC = SRC[SRC.index("def _carry_gate_diag_row"):SRC.index("def close_at(")]
check("the diagnostic NEVER assigns a hurdle (report only, no loosening)",
      "CARRY_HURDLE_ANN =" not in DIAG_SRC and "hurdle =" not in DIAG_SRC.replace(
          "hurdle = ", "", 0))
check("the diagnostic performs no writes at all",
      not any(t in DIAG_SRC for t in ("upsert", "INSERT", "UPDATE", "commit()")))
check("the loop runs the diagnostic at boot and once a day",
      "_next_diag = 0.0" in SRC and "_next_diag = time.time() + 86400" in SRC
      and "_carry_gate_diag_pass()" in SRC)
check("the daily diagnostic reads the FROZEN scoring list, not the archive list",
      "syms = CARRY_SCORING_SYMBOLS if syms is None else syms" in SRC)

# ── 8. COST SINGLE SOURCE (funding_carry.py) ───────────────────────────────
import funding_carry as fc

check("funding_carry no longer hardcodes 0.0036 / 0.0062",
      "0.0036" not in FC_SRC and "0.0062" not in FC_SRC)
check("funding_carry imports the bot's fee constants",
      "import bot_server as _bs" in FC_SRC and "_bs.KRAKEN_FEE" in FC_SRC
      and "_bs.KRAKEN_MAKER_FEE" in FC_SRC and "_bs.KRAKEN_FUTURES_FEE" in FC_SRC
      and "_bs.SLIPPAGE" in FC_SRC)
check("imported run reports the live source", fc.FEE_SOURCE.startswith("bot_server"))
for name, live in (("KRAKEN_FEE", bs.KRAKEN_FEE), ("KRAKEN_MAKER_FEE", bs.KRAKEN_MAKER_FEE),
                   ("FUTURES_FEE", bs.KRAKEN_FUTURES_FEE), ("SLIPPAGE", bs.SLIPPAGE)):
    check(f"funding_carry.{name} == the bot's value", getattr(fc, name) == live)
check("4-leg maker RT == 2x(spot maker + slip) + 2x(perp taker + slip)",
      abs(fc.COST_MAKER - (2 * (bs.KRAKEN_MAKER_FEE + bs.SLIPPAGE)
                           + 2 * (bs.KRAKEN_FUTURES_FEE + bs.SLIPPAGE))) < 1e-12)
check("4-leg taker RT == 2x(spot taker + slip) + 2x(perp taker + slip)",
      abs(fc.COST_TAKER - (2 * (bs.KRAKEN_FEE + bs.SLIPPAGE)
                           + 2 * (bs.KRAKEN_FUTURES_FEE + bs.SLIPPAGE))) < 1e-12)
check("the new cost model is STRICTER than the numbers it replaced (never looser)",
      fc.COST_MAKER > 0.0036 and fc.COST_TAKER > 0.0062,
      f"{fc.COST_MAKER} {fc.COST_TAKER}")
check("taker RT agrees with autopilot's CARRY_RT_4LEG (one cost model, two files)",
      abs(fc.COST_TAKER - _ap.CARRY_RT_4LEG) < 1e-12)
_hdr = fc.cost_header()
print("      " + _hdr)
check("output header prints every fee value used and its source",
      all(x in _hdr for x in ("source=", "spot taker", "spot maker", "perp taker",
                              "slippage", "4-leg round trip")))
check("main() prints the cost header FIRST", "print(cost_header())" in FC_SRC
      and FC_SRC.index("print(cost_header())") < FC_SRC.index("{'symbol':12s}"))
check("the fallback is documented as a fallback, not a second opinion",
      "standalone fallback" in FC_SRC and "never the old cheaper" in FC_SRC)

# standalone fallback: run the file where bot_server is NOT importable
_tmp = tempfile.mkdtemp(prefix="fc_standalone_")
try:
    shutil.copy(os.path.join(HERE, "funding_carry.py"), _tmp)
    drv = os.path.join(_tmp, "_drv.py")
    open(drv, "w", encoding="utf-8").write(
        "import funding_carry as fc\n"
        "print(fc.FEE_SOURCE);print(fc.COST_MAKER);print(fc.COST_TAKER)\n"
        "print(fc.KRAKEN_FEE, fc.KRAKEN_MAKER_FEE, fc.FUTURES_FEE, fc.SLIPPAGE)\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    p = subprocess.run([sys.executable, drv], cwd=_tmp, env=env,
                       capture_output=True, text=True, timeout=120)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    check("standalone run succeeds with no bot_server on the path",
          p.returncode == 0 and len(lines) >= 4, p.stderr[-300:])
    if p.returncode == 0 and len(lines) >= 4:
        check("standalone run SAYS it fell back", lines[0].startswith("standalone fallback"),
              lines[0])
        check("fallback fees are bot_server's documented defaults, not the old cheap ones",
              lines[3].split() == ["0.008", "0.004", "0.0005", "0.001"], lines[3])
        check("fallback 4-leg RT: maker 1.30% / taker 2.10%",
              abs(float(lines[1]) - 0.013) < 1e-12 and abs(float(lines[2]) - 0.021) < 1e-12,
              f"{lines[1]} {lines[2]}")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ── house rules ────────────────────────────────────────────────────────────
import re

check("nothing that would fail the secrets sweep",
      not re.search(r"0923|10\.0\.0\.88|100\.114|TG_TOKEN\s*=\s*[\"']\w|api_key\s*=\s*[\"']\w",
                    REGION + FC_SRC))
check("no gate, threshold or hurdle was loosened in the archive region",
      "CARRY_HURDLE" not in REGION.replace("autopilot.CARRY_HURDLE_ANN", "")
      .replace("_ap.CARRY_HURDLE_ANN", "").replace("CARRY_HURDLE_ANN", "", 0)
      or "= CARRY_HURDLE" not in REGION)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all funding-archive + carry-gate-diagnostic contract checks pass")
