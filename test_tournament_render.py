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

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all tournament-render / spread-map hook checks pass")
